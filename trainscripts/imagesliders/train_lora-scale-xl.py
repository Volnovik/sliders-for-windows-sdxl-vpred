# ref:
# - https://github.com/huggingface/diffusers/blob/main/src/diffusers/pipelines/stable_diffusion/pipeline_stable_diffusion.py#L566
# - https://huggingface.co/spaces/baulab/Erasing-Concepts-In-Diffusion/blob/main/train.py

from typing import List, Optional
import argparse
import ast
from pathlib import Path
import gc, os
import numpy as np

import torch
from tqdm import tqdm
from PIL import Image

from sai_model_spec import build_metadata
import time
import train_util
import random
import model_util
import prompt_util
from prompt_util import (
    PromptEmbedsCache,
    PromptEmbedsPair,
    PromptSettings,
    PromptEmbedsXL,
)
import debug_util
import config_util
from config_util import RootConfig

import wandb

NUM_IMAGES_PER_PROMPT = 1
from lora import LoRANetwork, DEFAULT_TARGET_REPLACE, UNET_TARGET_REPLACE_MODULE_CONV
used_indices = set()

def flush():
    torch.cuda.empty_cache()
    gc.collect()

def train(
    config: RootConfig,
    prompts: list[PromptSettings],
    device,
    folder_main: str,
    folders,
    scales,
):
    scales = np.array(scales)
    folders = np.array(folders)
    scales_unique = list(scales)

    metadata = {
        "prompts": ",".join([prompt.json() for prompt in prompts]),
        "config": config.json(),
    }
    save_path = Path(config.save.path)

    modules = DEFAULT_TARGET_REPLACE
    if config.network.type == "c3lier":
        modules += UNET_TARGET_REPLACE_MODULE_CONV

    if config.logging.verbose:
        print(metadata)

    if config.logging.use_wandb:
        wandb.init(project=f"LECO_{config.save.name}", config=metadata)

    metadata.update(
        build_metadata(
            v2=config.pretrained_model.v2,
            v_parameterization=config.pretrained_model.v_pred,
            sdxl=True,
            timestamp=time.time(),
            title="imagesliders",
        )
    )

    weight_dtype = config_util.parse_precision(config.train.precision)
    save_weight_dtype = config_util.parse_precision(config.train.precision)

    (tokenizers, text_encoders, unet, noise_scheduler, vae) = model_util.load_models_xl(
        config.pretrained_model.name_or_path,
        scheduler_name=config.train.noise_scheduler,
        v_pred=config.pretrained_model.v_pred,
        weight_dtype = weight_dtype,
        variant= "fp16" if weight_dtype == torch.float16 else None
    )

    for text_encoder in text_encoders:
        text_encoder.to(device, dtype=weight_dtype)
        text_encoder.requires_grad_(False)
        text_encoder.eval()

    unet.to(device, dtype=weight_dtype)
    if config.other.use_xformers:
        unet.enable_xformers_memory_efficient_attention()
    unet.requires_grad_(False)
    unet.eval()

    vae.to(device)
    vae.requires_grad_(False)
    vae.eval()

    network = LoRANetwork(
        unet,
        rank=config.network.rank,
        multiplier=1.0,
        alpha=config.network.alpha,
        train_method=config.network.training_method,
    ).to(device, dtype=weight_dtype)

    optimizer_module = train_util.get_optimizer(config.train.optimizer)
    # optimizer_args
    optimizer_kwargs = {}
    if config.train.optimizer_args is not None and len(config.train.optimizer_args) > 0:
        for arg in config.train.optimizer_args.split(" "):
            key, value = arg.split("=")
            value = ast.literal_eval(value)
            optimizer_kwargs[key] = value

    optimizer = optimizer_module(
        network.prepare_optimizer_params(), lr=config.train.lr, **optimizer_kwargs
    )
    lr_scheduler = train_util.get_lr_scheduler(
        config.train.lr_scheduler,
        optimizer,
        max_iterations=config.train.iterations,
        lr_min=config.train.lr / 100,
    )
    criteria = torch.nn.MSELoss()

    print("Prompts")
    for settings in prompts:
        print(settings)

    # debug
    debug_util.check_requires_grad(network)
    debug_util.check_training_mode(network)

    cache = PromptEmbedsCache()
    prompt_pairs: list[PromptEmbedsPair] = []

    with torch.no_grad():
        for settings in prompts:
            print(settings)
            for prompt in [
                settings.target,
                settings.positive,
                settings.neutral,
                settings.unconditional,
            ]:
                if cache[prompt] == None:
                    tex_embs, pool_embs = train_util.encode_prompts_xl(
                        tokenizers,
                        text_encoders,
                        [prompt],
                        num_images_per_prompt=NUM_IMAGES_PER_PROMPT,
                    )
                    cache[prompt] = PromptEmbedsXL(tex_embs, pool_embs)

            prompt_pairs.append(
                PromptEmbedsPair(
                    criteria,
                    cache[settings.target],
                    cache[settings.positive],
                    cache[settings.unconditional],
                    cache[settings.neutral],
                    settings,
                )
            )

    for tokenizer, text_encoder in zip(tokenizers, text_encoders):
        del tokenizer, text_encoder

    flush()

    pbar = tqdm(range(config.train.iterations))

    loss = None

    def get_next_random_index(ims):
        global used_indices
        if len(used_indices) == len(ims):
            used_indices.clear()  # 所有索引都已使用,重置集合
                
        while True:
            index = random.randint(0, len(ims) - 1)
            if index not in used_indices:
                used_indices.add(index)
                return index

    for i in pbar:
        with torch.no_grad():
            noise_scheduler.set_timesteps(
                config.train.max_denoising_steps, device=device
            )

            optimizer.zero_grad()

            prompt_pair: PromptEmbedsPair = prompt_pairs[
                torch.randint(0, len(prompt_pairs), (1,)).item()
            ]

            # 1 ~ 49 からランダム
            timesteps_to = torch.randint(
                1, config.train.max_denoising_steps, (1,)
            ).item()

            height, width = prompt_pair.resolution, prompt_pair.resolution

            scale_to_look = abs(random.choice(list(scales_unique)))
            folder1 = folders[scales == -scale_to_look][0]
            folder2 = folders[scales == scale_to_look][0]

            ims = os.listdir(f"{folder_main}/{folder1}/")
            ims = [
                im_
                for im_ in ims
                if ".png" in im_ or ".jpg" in im_ or ".jpeg" in im_ or ".webp" in im_
            ]

            random_sampler = get_next_random_index(ims)

            img1 = (
                Image.open(f"{folder_main}/{folder1}/{ims[random_sampler]}")
                .convert("RGB")
            )

            img2 = (
                Image.open(f"{folder_main}/{folder2}/{ims[random_sampler]}")
                .convert("RGB")
            )

            seed = random.randint(0, 2 * 15)

            if prompt_pair.dynamic_resolution:
                height, width = train_util.bucket_resolution(
                    bucket_resolution=prompt_pair.resolution, 
                    img_resolution=img1.size if img1.size[0]*img1.size[1]<img2.size[0]*img2.size[1] else img2.size, 
                    multiple=32
                )

            img2, img1 = train_util.align_images(img2, img1, width, height)

            if config.logging.verbose:
                print("guidance_scale:", prompt_pair.guidance_scale)
                print("resolution:", prompt_pair.resolution)
                print("dynamic_resolution:", prompt_pair.dynamic_resolution)
                if prompt_pair.dynamic_resolution:
                    print("img1:", (img1.size[1], img1.size[0]))
                    print("img2:", (img2.size[1], img2.size[0]))
                print("batch_size:", prompt_pair.batch_size)
                print("dynamic_crops:", prompt_pair.dynamic_crops)

            # Apply guidance_rescale=0.7 if vpred
            guidance_rescale = 0.7 if config.pretrained_model.v_pred else 0.0

            generator = torch.manual_seed(seed)
            denoised_latents_low, low_noise = train_util.get_noisy_image(
                img1,
                vae,
                generator,
                unet,
                noise_scheduler,
                start_timesteps=0,
                total_timesteps=timesteps_to,
            )
            denoised_latents_low = denoised_latents_low.to(device, dtype=weight_dtype)
            low_noise = low_noise.to(device, dtype=weight_dtype)

            generator = torch.manual_seed(seed)
            denoised_latents_high, high_noise = train_util.get_noisy_image(
                img2,
                vae,
                generator,
                unet,
                noise_scheduler,
                start_timesteps=0,
                total_timesteps=timesteps_to,
            )
            denoised_latents_high = denoised_latents_high.to(device, dtype=weight_dtype)
            high_noise = high_noise.to(device, dtype=weight_dtype)
            noise_scheduler.set_timesteps(1000)

            add_time_ids = train_util.get_add_time_ids(
                height,
                width,
                dynamic_crops=prompt_pair.dynamic_crops,
                dtype=weight_dtype,
            ).to(device, dtype=weight_dtype)

            current_timestep = noise_scheduler.timesteps[
                int(timesteps_to * 1000 / config.train.max_denoising_steps)
            ]
            # try:
            #     # with network: の外では空のLoRAのみが有効になる
            #     high_latents = train_util.predict_noise_xl(
            #         unet,
            #         noise_scheduler,
            #         current_timestep,
            #         denoised_latents_high,
            #         text_embeddings=train_util.concat_embeddings(
            #             prompt_pair.unconditional.text_embeds,
            #             prompt_pair.positive.text_embeds,
            #             prompt_pair.batch_size,
            #         ),
            #         add_text_embeddings=train_util.concat_embeddings(
            #             prompt_pair.unconditional.pooled_embeds,
            #             prompt_pair.positive.pooled_embeds,
            #             prompt_pair.batch_size,
            #         ),
            #         add_time_ids=train_util.concat_embeddings(
            #             add_time_ids, add_time_ids, prompt_pair.batch_size
            #         ),
            #         guidance_scale=1,
            #     ).to(device, dtype=torch.float32)
            # except:
            #     flush()
            #     print(f"Error Occured!: {np.array(img1).shape} {np.array(img2).shape}")
            #     continue
            # # with network: の外では空のLoRAのみが有効になる

            # low_latents = train_util.predict_noise_xl(
            #     unet,
            #     noise_scheduler,
            #     current_timestep,
            #     denoised_latents_low,
            #     text_embeddings=train_util.concat_embeddings(
            #         prompt_pair.unconditional.text_embeds,
            #         prompt_pair.neutral.text_embeds,
            #         prompt_pair.batch_size,
            #     ),
            #     add_text_embeddings=train_util.concat_embeddings(
            #         prompt_pair.unconditional.pooled_embeds,
            #         prompt_pair.neutral.pooled_embeds,
            #         prompt_pair.batch_size,
            #     ),
            #     add_time_ids=train_util.concat_embeddings(
            #         add_time_ids, add_time_ids, prompt_pair.batch_size
            #     ),
            #     guidance_scale=1,
            # ).to(device, dtype=torch.float32)

        network.set_lora_slider(scale=scale_to_look)
        with network:
            target_latents_high = train_util.predict_noise_xl(
                unet,
                noise_scheduler,
                current_timestep,
                denoised_latents_high,
                text_embeddings=train_util.concat_embeddings(
                    prompt_pair.unconditional.text_embeds,
                    prompt_pair.positive.text_embeds,
                    prompt_pair.batch_size,
                ),
                add_text_embeddings=train_util.concat_embeddings(
                    prompt_pair.unconditional.pooled_embeds,
                    prompt_pair.positive.pooled_embeds,
                    prompt_pair.batch_size,
                ),
                add_time_ids=train_util.concat_embeddings(
                    add_time_ids, add_time_ids, prompt_pair.batch_size
                ),
                guidance_scale=prompt_pair.guidance_scale,
                guidance_rescale=guidance_rescale,
            ).to(device, dtype=torch.float32)

        # high_latents.requires_grad = False
        # low_latents.requires_grad = False

        loss_high = criteria(target_latents_high, high_noise.to(torch.float32))
        pbar.set_description(f"High_Loss*1k: {loss_high.item()*1000:.4f}")
        loss_high.backward()

        # opposite
        network.set_lora_slider(scale=-scale_to_look)
        with network:
            target_latents_low = train_util.predict_noise_xl(
                unet,
                noise_scheduler,
                current_timestep,
                denoised_latents_low,
                text_embeddings=train_util.concat_embeddings(
                    prompt_pair.unconditional.text_embeds,
                    prompt_pair.neutral.text_embeds,
                    prompt_pair.batch_size,
                ),
                add_text_embeddings=train_util.concat_embeddings(
                    prompt_pair.unconditional.pooled_embeds,
                    prompt_pair.neutral.pooled_embeds,
                    prompt_pair.batch_size,
                ),
                add_time_ids=train_util.concat_embeddings(
                    add_time_ids, add_time_ids, prompt_pair.batch_size
                ),
                guidance_scale=prompt_pair.guidance_scale,
                guidance_rescale=guidance_rescale,
            ).to(device, dtype=torch.float32)

        # high_latents.requires_grad = False
        # low_latents.requires_grad = False

        loss_low = criteria(target_latents_low, low_noise.to(torch.float32))
        pbar.set_description(f"Low_Loss*1k: {loss_low.item()*1000:.4f}")
        loss_low.backward()

        if config.logging.verbose:
            print("high_latents:", target_latents_high[0, 0, :5, :5])
            print("low_latents:", target_latents_low[0, 0, :5, :5])

        optimizer.step()
        lr_scheduler.step()

        del (
            # high_latents,
            # low_latents,
            target_latents_low,
            target_latents_high,
            denoised_latents_low,
            denoised_latents_high,
            high_noise,
            low_noise,
        )
        flush()

        if (
            i % config.save.per_steps == 0
            and i != 0
            and i != config.train.iterations - 1
        ):
            print("Saving...")
            save_path.mkdir(parents=True, exist_ok=True)
            network.save_weights(
                save_path / f"{config.save.name}_{i}steps.safetensors",
                dtype=save_weight_dtype,
                metadata=metadata,
            )

    print("Saving...")
    save_path.mkdir(parents=True, exist_ok=True)
    network.save_weights(
        save_path / f"{config.save.name}_last.safetensors",
        dtype=save_weight_dtype,
        metadata=metadata,
    )

    del (
        unet,
        noise_scheduler,
        loss,
        optimizer,
        network,
    )

    flush()

    print("Done.")


def main(args):
    config_file = args.config_file

    config = config_util.load_config_from_yaml(config_file)
    if args.name is not None:
        config.save.name = args.name
    attributes = []
    if args.attributes is not None:
        attributes = args.attributes.split(",")
        attributes = [a.strip() for a in attributes]

    config.network.alpha = args.alpha
    config.network.rank = args.rank
    config.save.name += f"_alpha{args.alpha}"
    config.save.name += f"_rank{config.network.rank }"
    config.save.name += f"_{config.network.training_method}"
    config.save.path += f"/{config.save.name}"

    prompts = prompt_util.load_prompts_from_yaml(config.prompts_file, attributes)

    device = torch.device(f"cuda:{args.device}")

    folders = args.folders.split(",")
    folders = [f.strip() for f in folders]
    scales = args.scales.split(",")
    scales = [f.strip() for f in scales]
    scales = [int(s) for s in scales]

    print(folders, scales)
    if len(scales) != len(folders):
        raise Exception("the number of folders need to match the number of scales")

    if args.stylecheck is not None:
        check = args.stylecheck.split("-")

        for i in range(int(check[0]), int(check[1])):
            folder_main = args.folder_main + f"{i}"
            config.save.name = f"{os.path.basename(folder_main)}"
            config.save.name += f"_alpha{args.alpha}"
            config.save.name += f"_rank{config.network.rank }"
            config.save.path = f"models/{config.save.name}"
            train(
                config=config,
                prompts=prompts,
                device=device,
                folder_main=folder_main,
                folders=folders,
                scales=scales,
            )
    else:
        train(
            config=config,
            prompts=prompts,
            device=device,
            folder_main=args.folder_main,
            folders=folders,
            scales=scales,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config_file",
        required=True,
        help="Config file for training.",
    )
    # config_file 'data/config.yaml'
    parser.add_argument(
        "--alpha",
        type=float,
        required=True,
        help="LoRA weight.",
    )
    # --alpha 1.0
    parser.add_argument(
        "--rank",
        type=int,
        required=False,
        help="Rank of LoRA.",
        default=4,
    )
    # --rank 4
    parser.add_argument(
        "--device",
        type=int,
        required=False,
        default=0,
        help="Device to train on.",
    )
    # --device 0
    parser.add_argument(
        "--name",
        type=str,
        required=False,
        default=None,
        help="Device to train on.",
    )
    # --name 'eyesize_slider'
    parser.add_argument(
        "--attributes",
        type=str,
        required=False,
        default=None,
        help="attritbutes to disentangle (comma seperated string)",
    )
    parser.add_argument(
        "--folder_main",
        type=str,
        required=True,
        help="The folder to check",
    )

    parser.add_argument(
        "--stylecheck",
        type=str,
        required=False,
        default=None,
        help="The folder to check",
    )

    parser.add_argument(
        "--folders",
        type=str,
        required=False,
        default="verylow, low, high, veryhigh",
        help="folders with different attribute-scaled images",
    )
    parser.add_argument(
        "--scales",
        type=str,
        required=False,
        default="-2, -1, 1, 2",
        help="scales for different attribute-scaled images",
    )

    args = parser.parse_args()

    main(args)
