# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import torch
from diffusers import StableDiffusionXLPipeline, DDIMScheduler
from src import ptp_utils
import torch.nn as nn


def load_ldm(device, type="stabilityai/stable-diffusion-xl-base-1.0", feature_upsample_res=256, my_token=None):
    scheduler = DDIMScheduler(
        beta_start=0.00085,
        beta_end=0.012,
        beta_schedule="scaled_linear",
        clip_sample=False,
        set_alpha_to_one=False,
        steps_offset=1
    )

    NUM_DDIM_STEPS = 30
    scheduler.set_timesteps(NUM_DDIM_STEPS)

    ldm = StableDiffusionXLPipeline.from_pretrained(
        type, 
        token=my_token, 
        scheduler=scheduler,
        torch_dtype=torch.float16,
        variant="fp16",
        use_safetensors=True
    ).to(device)
    
    try:
        ldm.enable_xformers_memory_efficient_attention()
    except Exception as e:
        print(f"Warning: Could not enable xFormers memory efficient attention: {e}")
    
    if device != "cpu":
        ldm.unet = nn.DataParallel(ldm.unet)
        ldm.vae = nn.DataParallel(ldm.vae)
        
        controllers = {}
        for device_id in ldm.unet.device_ids:
            _device = torch.device("cuda", device_id)
            controller = ptp_utils.AttentionStore()
            controllers[_device] = controller
        effective_num_gpus = len(ldm.unet.device_ids)
    else:
        controllers = {}
        _device = torch.device("cpu")
        controller = ptp_utils.AttentionStore()
        controllers[_device] = controller
        effective_num_gpus = 1

        # patched_devices = set()

    def hook_fn(module, input):
        _device = input[0].device
        # if device not in patched_devices:
        ptp_utils.register_attention_control(module, controllers[_device], feature_upsample_res=feature_upsample_res)
        # patched_devices.add(device)

    if device != "cpu":
        ldm.unet.module.register_forward_pre_hook(hook_fn)
    else:
        ldm.unet.register_forward_pre_hook(hook_fn)
    
    for param in ldm.vae.parameters():
        param.requires_grad = False
    for param in ldm.text_encoder.parameters():
        param.requires_grad = False
    for param in ldm.text_encoder_2.parameters():
        param.requires_grad = False
    for param in ldm.unet.parameters():
        param.requires_grad = False

    return ldm, controllers, effective_num_gpus


def gaussian_circle(pos, size=64, sigma=16, device="cuda"):
    """Create a batch of 2D Gaussian circles with a given size, standard deviation, and center coordinates.

    pos is in between 0 and 1 and has shape [batch_size, 2]

    """
    batch_size = pos.shape[0]
    _pos = pos * size  # Shape [batch_size, 2]
    _pos = _pos.unsqueeze(1).unsqueeze(1)  # Shape [batch_size, 1, 1, 2]

    grid = torch.meshgrid(torch.arange(size).to(device), torch.arange(size).to(device))
    grid = torch.stack(grid, dim=-1) + 0.5  # Shape [size, size, 2]
    grid = grid.unsqueeze(0)  # Shape [1, size, size, 2]

    dist_sq = (grid[..., 1] - _pos[..., 1]) ** 2 + (
        grid[..., 0] - _pos[..., 0]
    ) ** 2  # Shape [batch_size, size, size]
    dist_sq = -1 * dist_sq / (2.0 * sigma**2.0)
    gaussian = torch.exp(dist_sq)  # Shape [batch_size, size, size]

    return gaussian


def gaussian_circles(pos, size=64, sigma=16, device="cuda"):
    """In the case of multiple points, pos has shape [batch_size, num_points, 2]
    """
    
    circles = []

    for i in range(pos.shape[0]):
        _circles = gaussian_circle(
            pos[i], size=size, sigma=sigma, device=device
        )  # Assuming H and W are the same
        
        circles.append(_circles)
        
    circles = torch.stack(circles)
    circles = torch.mean(circles, dim=0)
    
    return circles
