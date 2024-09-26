# Copyright 2024-present the HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from copy import deepcopy

import torch
import torch.nn.functional as F
from torch import nn

from peft.utils.integrations import dequantize_module_weight, gather_params_ctx
from peft.utils.other import transpose


class DoraLinearLayer(nn.Module):
    def __init__(self, fan_in_fan_out):
        super().__init__()
        self.fan_in_fan_out = fan_in_fan_out
        self.dora_num_dims = None
        self.shape = None

    def make_weight(self, A: torch.Tensor, B: torch.Tensor):
        """Layer-type-independent way of creating a weight matrix from LoRA A/B.

        While linear layer types are a straightforward matrix multiplication of
        the weights, convolution is a little less straightforward. This function
        will take a PEFT A/B matrix and return the full-sized weight matrix.

        A should be the equivalent of "LoRA Down" in most code, and likewise B
        the equivalent of "LoRA Up".

        Thanks to KohakuBlueLeaf (author of LyCORIS) for showing me how to do
        this in a layer-independent fashion. I was tearing my hair out over
        wrangling the matrix shapes in a functionally correct manner before.
        """
        W = B.view(B.size(0), -1) @ A.view(A.size(0), -1)
        return W.view(self.shape)

    def get_weight_shape(self, module: nn.Module) -> torch.Size:
        import bitsandbytes as bnb
        param = module.weight

        if isinstance(param, bnb.nn.Params4bit):
            if param.quant_state is not None:
                return param.quant_state.shape
            else:
                return param.shape

        return param.shape


    def get_weight_norm(self, weight, lora_weight, scaling) -> torch.Tensor:
        # calculate L2 norm of weight matrix, column-wise
        weight = transpose(weight, self.fan_in_fan_out)
        weight = weight + scaling * lora_weight
        weight_norm = torch.linalg.norm(weight, dim=1).to(weight.dtype)
        return weight_norm

    def update_layer(self, *, base_layer, lora_A, lora_B, scaling, place_on_cpu=False) -> None:
        # temporarily convert fp16 to fp32, as fp16 can cause trouble on CPU with PyTorch < 2.2
        dtype_is_fp16 = lora_A.dtype == torch.float16
        if dtype_is_fp16:
            lora_A = lora_A.float()
            lora_B = lora_B.float()

        self.shape = self.get_weight_shape(base_layer)

        with gather_params_ctx(base_layer.parameters()):
            if base_layer.__class__.__name__ == "Linear4bit":
                # We have to create a copy of the base layer, otherwise, FSDP will throw an error. 8bit does not work
                # yet because Int8Params cannot be correctly deep-copied (attributes vanish)
                base_layer = deepcopy(base_layer)

            weight = dequantize_module_weight(base_layer)
            weight = transpose(weight, self.fan_in_fan_out)
            self.dora_num_dims = weight.dim() - 1
            weight_norm = torch.norm(
                weight.transpose(1, 0).reshape(weight.shape[1], -1),
                dim=1, keepdim=True).reshape(weight.shape[1], *[1] * self.dora_num_dims).transpose(1, 0).to(device=lora_A.device)
            if dtype_is_fp16:
                weight_norm = weight_norm.half()

        if place_on_cpu:
            weight_norm = weight_norm.to("cpu")
        self.weight = nn.Parameter(weight_norm, requires_grad=True)

    def forward(self, x, *, lora_A, lora_B, scaling, base_layer):
        """
        For DoRA, calculate the extra output from LoRA with DoRA applied. This should be added on top of the base layer
        output.
        """
        lora_result = lora_B(lora_A(x))
        # Don't use `lora_weight = lora_B.weight @ lora_A.weight` because this causes errors with FSDP. Instead,
        # calculate the same but using forward.
        magnitude = self.weight
        weight = dequantize_module_weight(base_layer)
        weight = weight.to(x.dtype)
        weight_norm = weight + (self.make_weight(lora_A.weight, lora_B.weight) * scaling)
        print("weight_norm")
        print(weight_norm)
        print(weight_norm.shape)
        # see section 4.3 of DoRA (https://arxiv.org/abs/2402.09353)
        # "[...] we suggest treating ||V +∆V ||_c in
        # Eq. (5) as a constant, thereby detaching it from the gradient
        # graph. This means that while ||V + ∆V ||_c dynamically
        # reflects the updates of ∆V , it won’t receive any gradient
        # during backpropagation"
        weight_norm = weight_norm.detach()
        bb = (magnitude / weight_norm)
        print("bb")
        print(bb.shape)
        print("weight shape")
        print(weight.shape)
        mag_norm_scale = bb.view(-1, weight.shape[0])
        print("mag_norm")
        print(mag_norm_scale.shape)
        #mag_norm_scale = (magnitude / weight_norm).view(1, -1)
        result_dora = (mag_norm_scale - 1) * (
            F.linear(x, transpose(weight, self.fan_in_fan_out))
        ) + mag_norm_scale * lora_result * scaling

        # Note: Computation could potentially be accelerated by using the code below instead of calculating X@W again.
        # This is only correct if dropout=0, otherwise results will differ:
        # https://github.com/huggingface/peft/pull/1474#issuecomment-1964682771
        # bias = self.get_base_layer().bias
        # if bias is not None:
        #     result = result - bias
        # result = mag_norm_scale * result + mag_norm_scale * lora_B(lora_A(x)) * scaling
        # if bias is not None:
        #     result = result + bias

        return result_dora

    def __repr__(self) -> str:
        rep = super().__repr__()
        return "lora.dora." + rep


class DoraEmbeddingLayer(DoraLinearLayer):
    def forward(self, x, *, lora_A, lora_B, scaling, base_layer, embed_fn):
        """
        For DoRA, calculate the extra output from LoRA with DoRA applied. This should be added on top of the base layer
        output.
        """
        lora_weight = self.make_weight(lora_A.weight, lora_B.weight)
        magnitude = self.weight
        weight = base_layer.weight
        weight_norm = weight + (lora_weight * scaling)
        #weight_norm = self.get_weight_norm(weight, lora_weight.detach(), scaling)
        # see section 4.3 of DoRA (https://arxiv.org/abs/2402.09353)
        # "[...] we suggest treating ||V +∆V ||_c in
        # Eq. (5) as a constant, thereby detaching it from the gradient
        # graph. This means that while ||V + ∆V ||_c dynamically
        # reflects the updates of ∆V , it won’t receive any gradient
        # during backpropagation"
        eps = torch.finfo(weight_norm.dtype).eps
        norm = weight_norm.detach() \
                 .transpose(0, 1) \
                 .reshape(weight_norm.shape[1], -1) \
                 .norm(dim=1, keepdim=True) \
                 .reshape(weight_norm.shape[1], *[1] * self.dora_num_dims) \
                 .transpose(0, 1) + eps
        mag_norm_scale = magnitude / norm
        result_dora = mag_norm_scale * (embed_fn(x, lora_A) @ lora_B) * scaling
        return mag_norm_scale, result_dora

    def __repr__(self) -> str:
        rep = super().__repr__()
        return "lora.dora." + rep


class DoraConv2dLayer(DoraLinearLayer):
    def get_weight_norm(self, weight, lora_weight, scaling) -> torch.Tensor:
        # calculate L2 norm of weight matrix, column-wise
        weight = weight + scaling * lora_weight
        # the following is needed to have compatibility with the 4D weight tensors of Conv2D
        weight_norm = weight.norm(p=2, dim=(1, 2, 3), keepdim=True).transpose(1, 0)
        return weight_norm

    def forward(self, x, *, lora_A, lora_B, scaling, base_layer):
        """
        For DoRA, calculate the extra output from LoRA with DoRA applied. This should be added on top of the base layer
        output.
        """
        print("conv2dforward")
        weight = base_layer.weight
        #lora_weight = torch.mm(lora_B.weight.flatten(start_dim=1), lora_A.weight.flatten(start_dim=1))
        lora_weight = self.make_weight(lora_A.weight, lora_B.weight)
        magnitude = self.weight
        weight_norm = weight + (lora_weight * scaling)
        del weight
        #weight_norm = self.get_weight_norm(weight, lora_weight.detach(), scaling)
        # see section 4.3 of DoRA (https://arxiv.org/abs/2402.09353)
        # "[...] we suggest treating ||V +∆V ||_c in
        # Eq. (5) as a constant, thereby detaching it from the gradient
        # graph. This means that while ||V + ∆V ||_c dynamically
        # reflects the updates of ∆V , it won’t receive any gradient
        # during backpropagation"
        eps = torch.finfo(weight_norm.dtype).eps
        norm = weight_norm.detach() \
                 .transpose(0, 1) \
                 .reshape(weight_norm.shape[1], -1) \
                 .norm(dim=1, keepdim=True) \
                 .reshape(weight_norm.shape[1], *[1] * self.dora_num_dims) \
                 .transpose(0, 1) + eps
        weight_norm = magnitude * (weight_norm / norm)
        result_dora = F.conv2d(
                x,
                weight_norm,
                bias=None,
                stride=base_layer.stride,
                padding=base_layer.padding,
                dilation=base_layer.dilation,
                groups=base_layer.groups,
            )

        return result_dora

    def __repr__(self) -> str:
        rep = super().__repr__()
        return "lora.dora." + rep
