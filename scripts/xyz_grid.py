from collections import namedtuple
from copy import copy
from itertools import permutations, chain
import random
import csv
import os.path
from io import StringIO
from PIL import Image
import numpy as np
import gc

import modules.scripts as scripts
import gradio as gr

from modules import (
    images,
    sd_samplers,
    processing,
    sd_models,
    sd_vae,
    sd_schedulers,
    errors,
)
from modules.processing import (
    process_images,
    Processed,
    StableDiffusionProcessingTxt2Img,
)
from modules.shared import opts, state
import modules.shared as shared
import modules.sd_samplers
import modules.sd_models
import modules.sd_vae
import re

from modules.ui_components import ToolButton

fill_values_symbol = "\U0001f4d2"  # 📒

AxisInfo = namedtuple("AxisInfo", ["axis", "values"])


def apply_field(field):
    def fun(p, x, xs):
        setattr(p, field, x)

    return fun


def apply_prompt(p, x, xs):
    if xs[0] not in p.prompt and xs[0] not in p.negative_prompt:
        raise RuntimeError(
            f"Prompt S/R did not find {xs[0]} in prompt or negative prompt."
        )

    p.prompt = p.prompt.replace(xs[0], x)
    p.negative_prompt = p.negative_prompt.replace(xs[0], x)


def apply_order(p, x, xs):
    token_order = []

    # Initially grab the tokens from the prompt, so they can be replaced in order of earliest seen
    for token in x:
        token_order.append((p.prompt.find(token), token))

    token_order.sort(key=lambda t: t[0])

    prompt_parts = []

    # Split the prompt up, taking out the tokens
    for _, token in token_order:
        n = p.prompt.find(token)
        prompt_parts.append(p.prompt[0:n])
        p.prompt = p.prompt[n + len(token) :]

    # Rebuild the prompt with the tokens in the order we want
    prompt_tmp = ""
    for idx, part in enumerate(prompt_parts):
        prompt_tmp += part
        prompt_tmp += x[idx]
    p.prompt = prompt_tmp + p.prompt


def confirm_samplers(p, xs):
    for x in xs:
        if x.lower() not in sd_samplers.samplers_map:
            raise RuntimeError(f"Unknown sampler: {x}")


def apply_checkpoint(p, x, xs):
    info = modules.sd_models.get_closet_checkpoint_match(x)
    if info is None:
        raise RuntimeError(f"Unknown checkpoint: {x}")
    p.override_settings["sd_model_checkpoint"] = info.name


def confirm_checkpoints(p, xs):
    for x in xs:
        if modules.sd_models.get_closet_checkpoint_match(x) is None:
            raise RuntimeError(f"Unknown checkpoint: {x}")


def confirm_checkpoints_or_none(p, xs):
    for x in xs:
        if x in (None, "", "None", "none"):
            continue

        if modules.sd_models.get_closet_checkpoint_match(x) is None:
            raise RuntimeError(f"Unknown checkpoint: {x}")


def confirm_range(min_val, max_val, axis_label):
    """Generates a AxisOption.confirm() function that checks all values are within the specified range."""

    def confirm_range_fun(p, xs):
        for x in xs:
            if not (max_val >= x >= min_val):
                raise ValueError(
                    f'{axis_label} value "{x}" out of range [{min_val}, {max_val}]'
                )

    return confirm_range_fun


def apply_size(p, x: str, xs) -> None:
    try:
        width, _, height = x.partition("x")
        width = int(width.strip())
        height = int(height.strip())
        p.width = width
        p.height = height
    except ValueError:
        print(f"Invalid size in XYZ plot: {x}")


def find_vae(name: str):
    if (name := name.strip().lower()) in ("auto", "automatic"):
        return "Automatic"
    elif name == "none":
        return "None"
    return next(
        (k for k in modules.sd_vae.vae_dict if k.lower() == name),
        print(f"No VAE found for {name}; using Automatic") or "Automatic",
    )


def apply_vae(p, x, xs):
    p.override_settings["sd_vae"] = find_vae(x)


def apply_styles(p: StableDiffusionProcessingTxt2Img, x: str, _):
    p.styles.extend(x.split(","))


def apply_uni_pc_order(p, x, xs):
    p.override_settings["uni_pc_order"] = min(x, p.steps - 1)


def apply_face_restore(p, opt, x):
    opt = opt.lower()
    if opt == "codeformer":
        is_active = True
        p.face_restoration_model = "CodeFormer"
    elif opt == "gfpgan":
        is_active = True
        p.face_restoration_model = "GFPGAN"
    else:
        is_active = opt in ("true", "yes", "y", "1")

    p.restore_faces = is_active


def apply_override(field, boolean: bool = False):
    def fun(p, x, xs):
        if boolean:
            x = True if x.lower() == "true" else False
        p.override_settings[field] = x

    return fun


def boolean_choice(reverse: bool = False):
    def choice():
        return ["False", "True"] if reverse else ["True", "False"]

    return choice


def format_value_add_label(p, opt, x):
    if type(x) == float:
        x = round(x, 8)

    return f"{opt.label}: {x}"


def format_value(p, opt, x):
    if type(x) == float:
        x = round(x, 8)
    return x


def format_value_join_list(p, opt, x):
    return ", ".join(x)


def do_nothing(p, x, xs):
    pass


def format_nothing(p, opt, x):
    return ""


def format_remove_path(p, opt, x):
    return os.path.basename(x)


def str_permutations(x):
    """dummy function for specifying it in AxisOption's type when you want to get a list of permutations"""
    return x


def list_to_csv_string(data_list):
    with StringIO() as o:
        csv.writer(o).writerow(data_list)
        return o.getvalue().strip()


def csv_string_to_list_strip(data_str):
    return list(
        map(
            str.strip,
            chain.from_iterable(csv.reader(StringIO(data_str), skipinitialspace=True)),
        )
    )


class AxisOption:
    def __init__(
        self,
        label,
        type,
        apply,
        format_value=format_value_add_label,
        confirm=None,
        cost=0.0,
        choices=None,
        prepare=None,
    ):
        self.label = label
        self.type = type
        self.apply = apply
        self.format_value = format_value
        self.confirm = confirm
        self.cost = cost
        self.prepare = prepare
        self.choices = choices


class AxisOptionImg2Img(AxisOption):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_img2img = True


class AxisOptionTxt2Img(AxisOption):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_img2img = False


axis_options = [
    AxisOption("Nothing", str, do_nothing, format_value=format_nothing),
    AxisOption("Seed", int, apply_field("seed")),
    AxisOption("Var. seed", int, apply_field("subseed")),
    AxisOption("Var. strength", float, apply_field("subseed_strength")),
    AxisOption("Steps", int, apply_field("steps")),
    AxisOptionTxt2Img("Hires steps", int, apply_field("hr_second_pass_steps")),
    AxisOption("CFG Scale", float, apply_field("cfg_scale")),
    AxisOptionImg2Img("Image CFG Scale", float, apply_field("image_cfg_scale")),
    AxisOption("Prompt S/R", str, apply_prompt, format_value=format_value),
    AxisOption(
        "Prompt order",
        str_permutations,
        apply_order,
        format_value=format_value_join_list,
    ),
    AxisOptionTxt2Img(
        "Sampler",
        str,
        apply_field("sampler_name"),
        format_value=format_value,
        confirm=confirm_samplers,
        choices=lambda: [
            x.name for x in sd_samplers.samplers if x.name not in opts.hide_samplers
        ],
    ),
    AxisOptionTxt2Img(
        "Hires sampler",
        str,
        apply_field("hr_sampler_name"),
        confirm=confirm_samplers,
        choices=lambda: [
            x.name
            for x in sd_samplers.samplers_for_img2img
            if x.name not in opts.hide_samplers
        ],
    ),
    AxisOptionImg2Img(
        "Sampler",
        str,
        apply_field("sampler_name"),
        format_value=format_value,
        confirm=confirm_samplers,
        choices=lambda: [
            x.name
            for x in sd_samplers.samplers_for_img2img
            if x.name not in opts.hide_samplers
        ],
    ),
    AxisOption(
        "Checkpoint name",
        str,
        apply_checkpoint,
        format_value=format_remove_path,
        confirm=confirm_checkpoints,
        cost=1.0,
        choices=lambda: sorted(sd_models.checkpoints_list, key=str.casefold),
    ),
    AxisOption("Negative Guidance minimum sigma", float, apply_field("s_min_uncond")),
    AxisOption("Sigma Churn", float, apply_field("s_churn")),
    AxisOption("Sigma min", float, apply_field("s_tmin")),
    AxisOption("Sigma max", float, apply_field("s_tmax")),
    AxisOption("Sigma noise", float, apply_field("s_noise")),
    AxisOption(
        "Schedule type",
        str,
        apply_field("scheduler"),
        choices=lambda: [x.label for x in sd_schedulers.schedulers],
    ),
    AxisOption("Schedule min sigma", float, apply_override("sigma_min")),
    AxisOption("Schedule max sigma", float, apply_override("sigma_max")),
    AxisOption("Schedule rho", float, apply_override("rho")),
    AxisOption("Skip Early CFG", float, apply_override("skip_early_cond")),
    AxisOption("Beta schedule alpha", float, apply_override("beta_dist_alpha")),
    AxisOption("Beta schedule beta", float, apply_override("beta_dist_beta")),
    AxisOption("Eta", float, apply_field("eta")),
    AxisOption("Clip skip", int, apply_override("CLIP_stop_at_last_layers")),
    AxisOption("Denoising", float, apply_field("denoising_strength")),
    AxisOption(
        "Initial noise multiplier", float, apply_field("initial_noise_multiplier")
    ),
    AxisOption("Extra noise", float, apply_override("img2img_extra_noise")),
    AxisOptionTxt2Img(
        "Hires upscaler",
        str,
        apply_field("hr_upscaler"),
        choices=lambda: [
            *shared.latent_upscale_modes,
            *[x.name for x in shared.sd_upscalers],
        ],
    ),
    AxisOptionImg2Img(
        "Cond. Image Mask Weight", float, apply_field("inpainting_mask_weight")
    ),
    AxisOption(
        "VAE",
        str,
        apply_vae,
        cost=0.7,
        choices=lambda: ["Automatic", "None"] + list(sd_vae.vae_dict),
    ),
    AxisOption(
        "Styles", str, apply_styles, choices=lambda: list(shared.prompt_styles.styles)
    ),
    AxisOption("UniPC Order", int, apply_uni_pc_order, cost=0.5),
    AxisOption("Face restore", str, apply_face_restore, format_value=format_value),
    AxisOption("Token merging ratio", float, apply_override("token_merging_ratio")),
    AxisOption(
        "Token merging ratio high-res", float, apply_override("token_merging_ratio_hr")
    ),
    AxisOption(
        "Always discard next-to-last sigma",
        str,
        apply_override("always_discard_next_to_last_sigma", boolean=True),
        choices=boolean_choice(reverse=True),
    ),
    AxisOption(
        "SGM noise multiplier",
        str,
        apply_override("sgm_noise_multiplier", boolean=True),
        choices=boolean_choice(reverse=True),
    ),
    AxisOption(
        "Refiner checkpoint",
        str,
        apply_field("refiner_checkpoint"),
        format_value=format_remove_path,
        confirm=confirm_checkpoints_or_none,
        cost=1.0,
        choices=lambda: ["None"] + sorted(sd_models.checkpoints_list, key=str.casefold),
    ),
    AxisOption("Refiner switch at", float, apply_field("refiner_switch_at")),
    AxisOption(
        "RNG source",
        str,
        apply_override("randn_source"),
        choices=lambda: ["GPU", "CPU", "NV"],
    ),
    AxisOption(
        "FP8 mode",
        str,
        apply_override("fp8_storage"),
        cost=0.9,
        choices=lambda: ["Disable", "Enable for SDXL", "Enable"],
    ),
    AxisOption("Size", str, apply_size),
]


def draw_xyz_grid(
    p,
    xs,
    ys,
    zs,
    x_labels,
    y_labels,
    z_labels,
    cell,
    draw_legend,
    draw_individual_labels,
    include_lone_images,
    include_sub_grids,
    first_axes_processed,
    second_axes_processed,
    margin_size,
):
    hor_texts = [[images.GridAnnotation(x)] for x in x_labels]
    ver_texts = [[images.GridAnnotation(y)] for y in y_labels]
    title_texts = [[images.GridAnnotation(z)] for z in z_labels]

    list_size = len(xs) * len(ys) * len(zs)

    processed_result = None

    state.job_count = list_size * p.n_iter

    @staticmethod
    def draw_label_on_image(image, text):
        from PIL import ImageDraw, ImageFont

        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.truetype("arial.ttf", 20)
        except:
            font = ImageFont.load_default()

        margin = 10

        # Split text into lines and calculate maximum width
        lines = text.split("\n")
        max_width = 0
        total_height = 0

        # Calculate total size needed for all lines
        for line in lines:
            try:
                left, top, right, bottom = draw.textbbox(
                    (margin, margin), line, font=font
                )
                width = right - left
                height = bottom - top
            except AttributeError:
                width = len(line) * 10
                height = 20

            max_width = max(max_width, width)
            total_height += height

        # Draw background rectangle for all lines
        draw.rectangle(
            [(margin, margin), (margin + max_width, margin + total_height)],
            fill="black",
        )

        # Draw each line of text
        current_height = margin
        for line in lines:
            draw.text((margin, current_height), line, fill="white", font=font)
            try:
                left, top, right, bottom = draw.textbbox(
                    (margin, margin), line, font=font
                )
                height = bottom - top
            except AttributeError:
                height = 20
            current_height += height

    def process_cell(x, y, z, ix, iy, iz):
        nonlocal processed_result

        def index(ix, iy, iz):
            return ix + iy * len(xs) + iz * len(xs) * len(ys)

        state.job = f"{index(ix, iy, iz) + 1} out of {list_size}"

        processed: Processed = cell(x, y, z, ix, iy, iz)

        if processed_result is None:
            # Use our first processed result object as a template container to hold our full results
            processed_result = copy(processed)
            processed_result.images = [None] * list_size
            processed_result.all_prompts = [None] * list_size
            processed_result.all_seeds = [None] * list_size
            processed_result.infotexts = [None] * list_size
            processed_result.index_of_first_image = 1

        idx = index(ix, iy, iz)
        if processed.images:
            # Non-empty list indicates some degree of success.
            process_image = processed.images[0]  # Store reference to image

            if draw_individual_labels:
                # Add labels to a copy of the image
                process_image = process_image.copy()  # Make a copy before drawing
                label = f"X: {x_labels[ix]}\nY: {y_labels[iy]}\nZ: {z_labels[iz]}"
                draw_label_on_image(process_image, label)

            processed_result.images[idx] = process_image
            processed_result.all_prompts[idx] = processed.prompt
            processed_result.all_seeds[idx] = processed.seed
            processed_result.infotexts[idx] = processed.infotexts[0]
        else:
            cell_mode = "P"
            cell_size = (processed_result.width, processed_result.height)
            if processed_result.images[0] is not None:
                cell_mode = processed_result.images[0].mode
                # This corrects size in case of batches:
                cell_size = processed_result.images[0].size
            processed_result.images[idx] = Image.new(cell_mode, cell_size)

    if first_axes_processed == "x":
        for ix, x in enumerate(xs):
            if second_axes_processed == "y":
                for iy, y in enumerate(ys):
                    for iz, z in enumerate(zs):
                        process_cell(x, y, z, ix, iy, iz)
            else:
                for iz, z in enumerate(zs):
                    for iy, y in enumerate(ys):
                        process_cell(x, y, z, ix, iy, iz)
    elif first_axes_processed == "y":
        for iy, y in enumerate(ys):
            if second_axes_processed == "x":
                for ix, x in enumerate(xs):
                    for iz, z in enumerate(zs):
                        process_cell(x, y, z, ix, iy, iz)
            else:
                for iz, z in enumerate(zs):
                    for ix, x in enumerate(xs):
                        process_cell(x, y, z, ix, iy, iz)
    elif first_axes_processed == "z":
        for iz, z in enumerate(zs):
            if second_axes_processed == "x":
                for ix, x in enumerate(xs):
                    for iy, y in enumerate(ys):
                        process_cell(x, y, z, ix, iy, iz)
            else:
                for iy, y in enumerate(ys):
                    for ix, x in enumerate(xs):
                        process_cell(x, y, z, ix, iy, iz)

    if not processed_result:
        print(
            "Unexpected error: Processing could not begin, you may need to refresh the tab or restart the service."
        )
        return Processed(p, [])
    elif not any(processed_result.images):
        print(
            "Unexpected error: draw_xyz_grid failed to return even a single processed image"
        )
        return Processed(p, [])

    z_count = len(zs)

    for i in range(z_count):
        start_index = (i * len(xs) * len(ys)) + i
        end_index = start_index + len(xs) * len(ys)
        grid = images.image_grid(
            processed_result.images[start_index:end_index], rows=len(ys)
        )
        if draw_legend:
            grid_max_w, grid_max_h = map(
                max,
                zip(
                    *(
                        img.size
                        for img in processed_result.images[start_index:end_index]
                    )
                ),
            )
            grid = images.draw_grid_annotations(
                grid, grid_max_w, grid_max_h, hor_texts, ver_texts, margin_size
            )
        processed_result.images.insert(i, grid)
        processed_result.all_prompts.insert(
            i, processed_result.all_prompts[start_index]
        )
        processed_result.all_seeds.insert(i, processed_result.all_seeds[start_index])
        processed_result.infotexts.insert(i, processed_result.infotexts[start_index])

    z_grid = images.image_grid(processed_result.images[:z_count], rows=1)
    z_sub_grid_max_w, z_sub_grid_max_h = map(
        max, zip(*(img.size for img in processed_result.images[:z_count]))
    )
    if draw_legend:
        z_grid = images.draw_grid_annotations(
            z_grid,
            z_sub_grid_max_w,
            z_sub_grid_max_h,
            title_texts,
            [[images.GridAnnotation()]],
        )
    processed_result.images.insert(0, z_grid)
    processed_result.infotexts.insert(0, processed_result.infotexts[0])

    return processed_result


class SharedSettingsStackHelper(object):
    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_value, tb):
        modules.sd_models.reload_model_weights()
        modules.sd_vae.reload_vae_weights()


re_range = re.compile(
    r"\s*([+-]?\s*\d+)\s*-\s*([+-]?\s*\d+)(?:\s*\(([+-]\d+)\s*\))?\s*"
)
re_range_float = re.compile(
    r"\s*([+-]?\s*\d+(?:.\d*)?)\s*-\s*([+-]?\s*\d+(?:.\d*)?)(?:\s*\(([+-]\d+(?:.\d*)?)\s*\))?\s*"
)

re_range_count = re.compile(
    r"\s*([+-]?\s*\d+)\s*-\s*([+-]?\s*\d+)(?:\s*\[(\d+)\s*])?\s*"
)
re_range_count_float = re.compile(
    r"\s*([+-]?\s*\d+(?:.\d*)?)\s*-\s*([+-]?\s*\d+(?:.\d*)?)(?:\s*\[(\d+(?:.\d*)?)\s*])?\s*"
)


class Script(scripts.Script):
    def title(self):
        return "X/Y/Z plot"

    def ui(self, is_img2img):
        self.current_axis_options = [
            x
            for x in axis_options
            if type(x) == AxisOption or x.is_img2img == is_img2img
        ]

        with gr.Row():
            with gr.Column(scale=19):
                with gr.Row():
                    x_type = gr.Dropdown(
                        label="X type",
                        choices=[x.label for x in self.current_axis_options],
                        value=self.current_axis_options[1].label,
                        type="index",
                        elem_id=self.elem_id("x_type"),
                    )
                    x_values = gr.Textbox(
                        label="X values", lines=1, elem_id=self.elem_id("x_values")
                    )
                    x_values_dropdown = gr.Dropdown(
                        label="X values",
                        visible=False,
                        multiselect=True,
                        interactive=True,
                    )
                    fill_x_button = ToolButton(
                        value=fill_values_symbol,
                        elem_id="xyz_grid_fill_x_tool_button",
                        visible=False,
                    )

                with gr.Row():
                    y_type = gr.Dropdown(
                        label="Y type",
                        choices=[x.label for x in self.current_axis_options],
                        value=self.current_axis_options[0].label,
                        type="index",
                        elem_id=self.elem_id("y_type"),
                    )
                    y_values = gr.Textbox(
                        label="Y values", lines=1, elem_id=self.elem_id("y_values")
                    )
                    y_values_dropdown = gr.Dropdown(
                        label="Y values",
                        visible=False,
                        multiselect=True,
                        interactive=True,
                    )
                    fill_y_button = ToolButton(
                        value=fill_values_symbol,
                        elem_id="xyz_grid_fill_y_tool_button",
                        visible=False,
                    )

                with gr.Row():
                    z_type = gr.Dropdown(
                        label="Z type",
                        choices=[x.label for x in self.current_axis_options],
                        value=self.current_axis_options[0].label,
                        type="index",
                        elem_id=self.elem_id("z_type"),
                    )
                    z_values = gr.Textbox(
                        label="Z values", lines=1, elem_id=self.elem_id("z_values")
                    )
                    z_values_dropdown = gr.Dropdown(
                        label="Z values",
                        visible=False,
                        multiselect=True,
                        interactive=True,
                    )
                    fill_z_button = ToolButton(
                        value=fill_values_symbol,
                        elem_id="xyz_grid_fill_z_tool_button",
                        visible=False,
                    )

        with gr.Row(variant="compact", elem_id="axis_options"):
            with gr.Column():
                draw_legend = gr.Checkbox(
                    label="Draw legend", value=True, elem_id=self.elem_id("draw_legend")
                )
                draw_individual_labels = gr.Checkbox(
                    label="Draw individual labels",
                    value=False,
                    elem_id=self.elem_id("draw_individual_labels"),
                )
                skip_grid = gr.Checkbox(
                    label="Skip final grid generation",
                    value=False,
                    elem_id=self.elem_id("skip_grid"),
                )
                items_per_grid = gr.Slider(
                    label="Items per grid (0 = default), for sequential grid generation.",
                    value=0,
                    minimum=0,
                    maximum=200,
                    step=1,
                    elem_id=self.elem_id("items_per_grid"),
                )
                no_fixed_seeds = gr.Checkbox(
                    label="Keep -1 for seeds",
                    value=False,
                    elem_id=self.elem_id("no_fixed_seeds"),
                )
                with gr.Row():
                    vary_seeds_x = gr.Checkbox(
                        label="Vary seeds for X",
                        value=False,
                        min_width=80,
                        elem_id=self.elem_id("vary_seeds_x"),
                        tooltip="Use different seeds for images along X axis.",
                    )
                    vary_seeds_y = gr.Checkbox(
                        label="Vary seeds for Y",
                        value=False,
                        min_width=80,
                        elem_id=self.elem_id("vary_seeds_y"),
                        tooltip="Use different seeds for images along Y axis.",
                    )
                    vary_seeds_z = gr.Checkbox(
                        label="Vary seeds for Z",
                        value=False,
                        min_width=80,
                        elem_id=self.elem_id("vary_seeds_z"),
                        tooltip="Use different seeds for images along Z axis.",
                    )
            with gr.Column():
                include_lone_images = gr.Checkbox(
                    label="Include Sub Images",
                    value=False,
                    elem_id=self.elem_id("include_lone_images"),
                )
                include_sub_grids = gr.Checkbox(
                    label="Include Sub Grids",
                    value=False,
                    elem_id=self.elem_id("include_sub_grids"),
                )
                csv_mode = gr.Checkbox(
                    label="Use text inputs instead of dropdowns",
                    value=False,
                    elem_id=self.elem_id("csv_mode"),
                )
            with gr.Column():
                margin_size = gr.Slider(
                    label="Grid margins (px)",
                    minimum=0,
                    maximum=500,
                    value=0,
                    step=2,
                    elem_id=self.elem_id("margin_size"),
                )

        # Add dependency for skip_grid to force include_lone_images
        def update_include_lone_images(skip_grid):
            return gr.update(
                value=True if skip_grid else include_lone_images.value,
                interactive=not skip_grid,
            )

        skip_grid.change(
            fn=update_include_lone_images,
            inputs=[skip_grid],
            outputs=[include_lone_images],
        )

        with gr.Row(variant="compact", elem_id="swap_axes"):
            swap_xy_axes_button = gr.Button(
                value="Swap X/Y axes", elem_id="xy_grid_swap_axes_button"
            )
            swap_yz_axes_button = gr.Button(
                value="Swap Y/Z axes", elem_id="yz_grid_swap_axes_button"
            )
            swap_xz_axes_button = gr.Button(
                value="Swap X/Z axes", elem_id="xz_grid_swap_axes_button"
            )

        def swap_axes(
            axis1_type,
            axis1_values,
            axis1_values_dropdown,
            axis2_type,
            axis2_values,
            axis2_values_dropdown,
        ):
            return (
                self.current_axis_options[axis2_type].label,
                axis2_values,
                axis2_values_dropdown,
                self.current_axis_options[axis1_type].label,
                axis1_values,
                axis1_values_dropdown,
            )

        xy_swap_args = [
            x_type,
            x_values,
            x_values_dropdown,
            y_type,
            y_values,
            y_values_dropdown,
        ]
        swap_xy_axes_button.click(swap_axes, inputs=xy_swap_args, outputs=xy_swap_args)
        yz_swap_args = [
            y_type,
            y_values,
            y_values_dropdown,
            z_type,
            z_values,
            z_values_dropdown,
        ]
        swap_yz_axes_button.click(swap_axes, inputs=yz_swap_args, outputs=yz_swap_args)
        xz_swap_args = [
            x_type,
            x_values,
            x_values_dropdown,
            z_type,
            z_values,
            z_values_dropdown,
        ]
        swap_xz_axes_button.click(swap_axes, inputs=xz_swap_args, outputs=xz_swap_args)

        def fill(axis_type, csv_mode):
            axis = self.current_axis_options[axis_type]
            if axis.choices:
                if csv_mode:
                    return list_to_csv_string(axis.choices()), gr.update()
                else:
                    return gr.update(), axis.choices()
            else:
                return gr.update(), gr.update()

        fill_x_button.click(
            fn=fill, inputs=[x_type, csv_mode], outputs=[x_values, x_values_dropdown]
        )
        fill_y_button.click(
            fn=fill, inputs=[y_type, csv_mode], outputs=[y_values, y_values_dropdown]
        )
        fill_z_button.click(
            fn=fill, inputs=[z_type, csv_mode], outputs=[z_values, z_values_dropdown]
        )

        def select_axis(axis_type, axis_values, axis_values_dropdown, csv_mode):
            axis_type = axis_type or 0  # if axle type is None set to 0

            choices = self.current_axis_options[axis_type].choices
            has_choices = choices is not None

            if has_choices:
                choices = choices()
                if csv_mode:
                    if axis_values_dropdown:
                        axis_values = list_to_csv_string(
                            list(filter(lambda x: x in choices, axis_values_dropdown))
                        )
                        axis_values_dropdown = []
                else:
                    if axis_values:
                        axis_values_dropdown = list(
                            filter(
                                lambda x: x in choices,
                                csv_string_to_list_strip(axis_values),
                            )
                        )
                        axis_values = ""

            return (
                gr.Button.update(visible=has_choices),
                gr.Textbox.update(
                    visible=not has_choices or csv_mode, value=axis_values
                ),
                gr.update(
                    choices=choices if has_choices else None,
                    visible=has_choices and not csv_mode,
                    value=axis_values_dropdown,
                ),
            )

        x_type.change(
            fn=select_axis,
            inputs=[x_type, x_values, x_values_dropdown, csv_mode],
            outputs=[fill_x_button, x_values, x_values_dropdown],
        )
        y_type.change(
            fn=select_axis,
            inputs=[y_type, y_values, y_values_dropdown, csv_mode],
            outputs=[fill_y_button, y_values, y_values_dropdown],
        )
        z_type.change(
            fn=select_axis,
            inputs=[z_type, z_values, z_values_dropdown, csv_mode],
            outputs=[fill_z_button, z_values, z_values_dropdown],
        )

        def change_choice_mode(
            csv_mode,
            x_type,
            x_values,
            x_values_dropdown,
            y_type,
            y_values,
            y_values_dropdown,
            z_type,
            z_values,
            z_values_dropdown,
        ):
            _fill_x_button, _x_values, _x_values_dropdown = select_axis(
                x_type, x_values, x_values_dropdown, csv_mode
            )
            _fill_y_button, _y_values, _y_values_dropdown = select_axis(
                y_type, y_values, y_values_dropdown, csv_mode
            )
            _fill_z_button, _z_values, _z_values_dropdown = select_axis(
                z_type, z_values, z_values_dropdown, csv_mode
            )
            return (
                _fill_x_button,
                _x_values,
                _x_values_dropdown,
                _fill_y_button,
                _y_values,
                _y_values_dropdown,
                _fill_z_button,
                _z_values,
                _z_values_dropdown,
            )

        csv_mode.change(
            fn=change_choice_mode,
            inputs=[
                csv_mode,
                x_type,
                x_values,
                x_values_dropdown,
                y_type,
                y_values,
                y_values_dropdown,
                z_type,
                z_values,
                z_values_dropdown,
            ],
            outputs=[
                fill_x_button,
                x_values,
                x_values_dropdown,
                fill_y_button,
                y_values,
                y_values_dropdown,
                fill_z_button,
                z_values,
                z_values_dropdown,
            ],
        )

        def get_dropdown_update_from_params(axis, params):
            val_key = f"{axis} Values"
            vals = params.get(val_key, "")
            valslist = csv_string_to_list_strip(vals)
            return gr.update(value=valslist)

        self.infotext_fields = (
            (x_type, "X Type"),
            (x_values, "X Values"),
            (
                x_values_dropdown,
                lambda params: get_dropdown_update_from_params("X", params),
            ),
            (y_type, "Y Type"),
            (y_values, "Y Values"),
            (
                y_values_dropdown,
                lambda params: get_dropdown_update_from_params("Y", params),
            ),
            (z_type, "Z Type"),
            (z_values, "Z Values"),
            (
                z_values_dropdown,
                lambda params: get_dropdown_update_from_params("Z", params),
            ),
        )

        return [
            x_type,
            x_values,
            x_values_dropdown,
            y_type,
            y_values,
            y_values_dropdown,
            z_type,
            z_values,
            z_values_dropdown,
            draw_legend,
            draw_individual_labels,
            skip_grid,
            items_per_grid,
            include_lone_images,
            include_sub_grids,
            no_fixed_seeds,
            vary_seeds_x,
            vary_seeds_y,
            vary_seeds_z,
            margin_size,
            csv_mode,
        ]

    def draw_label_on_image(image, text):
        from PIL import ImageDraw, ImageFont

        draw = ImageDraw.Draw(image)
        # You might want to adjust font size and position
        try:
            font = ImageFont.truetype("arial.ttf", 20)
        except:
            font = ImageFont.load_default()

        # Draw text with background for better visibility
        margin = 10
        text_width, text_height = draw.textsize(text, font=font)
        draw.rectangle(
            [(margin, margin), (margin + text_width, margin + text_height)],
            fill="black",
        )
        draw.text((margin, margin), text, fill="white", font=font)

    def run(
        self,
        p,
        x_type,
        x_values,
        x_values_dropdown,
        y_type,
        y_values,
        y_values_dropdown,
        z_type,
        z_values,
        z_values_dropdown,
        draw_legend,
        draw_individual_labels,
        skip_grid,
        items_per_grid,
        include_lone_images,
        include_sub_grids,
        no_fixed_seeds,
        vary_seeds_x,
        vary_seeds_y,
        vary_seeds_z,
        margin_size,
        csv_mode,
    ):
        x_type, y_type, z_type = (
            x_type or 0,
            y_type or 0,
            z_type or 0,
        )  # if axle type is None set to 0

        if not no_fixed_seeds:
            modules.processing.fix_seed(p)

        if not opts.return_grid:
            p.batch_size = 1

        if skip_grid:
            include_lone_images = True
            include_sub_grids = False

        def process_axis(opt, vals, vals_dropdown):
            if opt.label == "Nothing":
                return [0]

            if opt.choices is not None and not csv_mode:
                valslist = vals_dropdown
            elif opt.prepare is not None:
                valslist = opt.prepare(vals)
            else:
                valslist = csv_string_to_list_strip(vals)

            if opt.type == int:
                valslist_ext = []

                for val in valslist:
                    if val.strip() == "":
                        continue
                    m = re_range.fullmatch(val)
                    mc = re_range_count.fullmatch(val)
                    if m is not None:
                        start = int(m.group(1))
                        end = int(m.group(2)) + 1
                        step = int(m.group(3)) if m.group(3) is not None else 1

                        valslist_ext += list(range(start, end, step))
                    elif mc is not None:
                        start = int(mc.group(1))
                        end = int(mc.group(2))
                        num = int(mc.group(3)) if mc.group(3) is not None else 1

                        valslist_ext += [
                            int(x)
                            for x in np.linspace(
                                start=start, stop=end, num=num
                            ).tolist()
                        ]
                    else:
                        valslist_ext.append(val)

                valslist = valslist_ext
            elif opt.type == float:
                valslist_ext = []

                for val in valslist:
                    if val.strip() == "":
                        continue
                    m = re_range_float.fullmatch(val)
                    mc = re_range_count_float.fullmatch(val)
                    if m is not None:
                        start = float(m.group(1))
                        end = float(m.group(2))
                        step = float(m.group(3)) if m.group(3) is not None else 1

                        valslist_ext += np.arange(start, end + step, step).tolist()
                    elif mc is not None:
                        start = float(mc.group(1))
                        end = float(mc.group(2))
                        num = int(mc.group(3)) if mc.group(3) is not None else 1

                        valslist_ext += np.linspace(
                            start=start, stop=end, num=num
                        ).tolist()
                    else:
                        valslist_ext.append(val)

                valslist = valslist_ext
            elif opt.type == str_permutations:
                valslist = list(permutations(valslist))

            valslist = [opt.type(x) for x in valslist]

            # Confirm options are valid before starting
            if opt.confirm:
                opt.confirm(p, valslist)

            return valslist

        x_opt = self.current_axis_options[x_type]
        if x_opt.choices is not None and not csv_mode:
            x_values = list_to_csv_string(x_values_dropdown)
        xs = process_axis(x_opt, x_values, x_values_dropdown)

        y_opt = self.current_axis_options[y_type]
        if y_opt.choices is not None and not csv_mode:
            y_values = list_to_csv_string(y_values_dropdown)
        ys = process_axis(y_opt, y_values, y_values_dropdown)

        z_opt = self.current_axis_options[z_type]
        if z_opt.choices is not None and not csv_mode:
            z_values = list_to_csv_string(z_values_dropdown)
        zs = process_axis(z_opt, z_values, z_values_dropdown)

        # this could be moved to common code, but unlikely to be ever triggered anywhere else
        Image.MAX_IMAGE_PIXELS = None  # disable check in Pillow and rely on check below to allow large custom image sizes
        grid_mp = round(len(xs) * len(ys) * len(zs) * p.width * p.height / 1000000)
        assert grid_mp < opts.img_max_size_mp, (
            f"Error: Resulting grid would be too large ({grid_mp} MPixels) (max configured size is {opts.img_max_size_mp} MPixels)"
        )

        def fix_axis_seeds(axis_opt, axis_list):
            if axis_opt.label in ["Seed", "Var. seed"]:
                return [
                    int(random.randrange(4294967294))
                    if val is None or val == "" or val == -1
                    else val
                    for val in axis_list
                ]
            else:
                return axis_list

        if not no_fixed_seeds:
            xs = fix_axis_seeds(x_opt, xs)
            ys = fix_axis_seeds(y_opt, ys)
            zs = fix_axis_seeds(z_opt, zs)

        if x_opt.label == "Steps":
            total_steps = sum(xs) * len(ys) * len(zs)
        elif y_opt.label == "Steps":
            total_steps = sum(ys) * len(xs) * len(zs)
        elif z_opt.label == "Steps":
            total_steps = sum(zs) * len(xs) * len(ys)
        else:
            total_steps = p.steps * len(xs) * len(ys) * len(zs)

        if isinstance(p, StableDiffusionProcessingTxt2Img) and p.enable_hr:
            if x_opt.label == "Hires steps":
                total_steps += sum(xs) * len(ys) * len(zs)
            elif y_opt.label == "Hires steps":
                total_steps += sum(ys) * len(xs) * len(zs)
            elif z_opt.label == "Hires steps":
                total_steps += sum(zs) * len(xs) * len(ys)
            elif p.hr_second_pass_steps:
                total_steps += p.hr_second_pass_steps * len(xs) * len(ys) * len(zs)
            else:
                total_steps *= 2

        total_steps *= p.n_iter

        image_cell_count = p.n_iter * p.batch_size
        cell_console_text = (
            f"; {image_cell_count} images per cell" if image_cell_count > 1 else ""
        )
        plural_s = "s" if len(zs) > 1 else ""
        print(
            f"X/Y/Z plot will create {len(xs) * len(ys) * len(zs) * image_cell_count} images on {len(zs)} {len(xs)}x{len(ys)} grid{plural_s}{cell_console_text}. (Total steps to process: {total_steps})"
        )
        shared.total_tqdm.updateTotal(total_steps)

        state.xyz_plot_x = AxisInfo(x_opt, xs)
        state.xyz_plot_y = AxisInfo(y_opt, ys)
        state.xyz_plot_z = AxisInfo(z_opt, zs)

        # If one of the axes is very slow to change between (like SD model
        # checkpoint), then make sure it is in the outer iteration of the nested
        # `for` loop.
        first_axes_processed = "z"
        second_axes_processed = "y"
        if x_opt.cost > y_opt.cost and x_opt.cost > z_opt.cost:
            first_axes_processed = "x"
            if y_opt.cost > z_opt.cost:
                second_axes_processed = "y"
            else:
                second_axes_processed = "z"
        elif y_opt.cost > x_opt.cost and y_opt.cost > z_opt.cost:
            first_axes_processed = "y"
            if x_opt.cost > z_opt.cost:
                second_axes_processed = "x"
            else:
                second_axes_processed = "z"
        elif z_opt.cost > x_opt.cost and z_opt.cost > y_opt.cost:
            first_axes_processed = "z"
            if x_opt.cost > y_opt.cost:
                second_axes_processed = "x"
            else:
                second_axes_processed = "y"

        grid_infotext = [None] * (1 + len(zs))

        def cell(x, y, z, ix, iy, iz):
            if shared.state.interrupted or state.stopping_generation:
                return Processed(p, [], p.seed, "")

            pc = copy(p)
            pc.styles = pc.styles[:]
            x_opt.apply(pc, x, xs)
            y_opt.apply(pc, y, ys)
            z_opt.apply(pc, z, zs)

            xdim = len(xs) if vary_seeds_x else 1
            ydim = len(ys) if vary_seeds_y else 1
            if vary_seeds_x:
                pc.seed += ix
            if vary_seeds_y:
                pc.seed += iy * xdim
            if vary_seeds_z:
                pc.seed += iz * xdim * ydim

            try:
                res = process_images(pc)

                # If draw_individual_labels is enabled, save the labeled image immediately
                if draw_individual_labels and res.images:
                    # Create a copy of the image and add labels
                    labeled_image = res.images[0].copy()
                    label = f"X: {x_opt.format_value(p, x_opt, x)}\nY: {y_opt.format_value(p, y_opt, y)}\nZ: {z_opt.format_value(p, z_opt, z)}"

                    # Draw label directly here instead of using a separate method
                    from PIL import ImageDraw, ImageFont

                    draw = ImageDraw.Draw(labeled_image)
                    try:
                        font = ImageFont.truetype("arial.ttf", 20)
                    except:
                        font = ImageFont.load_default()

                    margin = 10
                    lines = label.split("\n")
                    max_width = 0
                    total_height = 0

                    # Calculate total size needed for all lines
                    for line in lines:
                        try:
                            left, top, right, bottom = draw.textbbox(
                                (margin, margin), line, font=font
                            )
                            width = right - left
                            height = bottom - top
                        except AttributeError:
                            width = len(line) * 10
                            height = 20

                        max_width = max(max_width, width)
                        total_height += height

                    # Draw background rectangle for all lines
                    draw.rectangle(
                        [(margin, margin), (margin + max_width, margin + total_height)],
                        fill="black",
                    )

                    # Draw each line of text
                    current_height = margin
                    for line in lines:
                        draw.text(
                            (margin, current_height), line, fill="white", font=font
                        )
                        try:
                            left, top, right, bottom = draw.textbbox(
                                (margin, margin), line, font=font
                            )
                            height = bottom - top
                        except AttributeError:
                            height = 20
                        current_height += height

                    # Generate a unique filename based on coordinates
                    filename = f"xyz_grid_x{ix}_y{iy}_z{iz}"

                    # Save the labeled image
                    if opts.grid_save:
                        images.save_image(
                            labeled_image,
                            p.outpath_grids,
                            filename,
                            info=res.infotexts[0],
                            extension=opts.grid_format,
                            prompt=res.all_prompts[0],
                            seed=res.all_seeds[0],
                            grid=False,
                            p=res,
                        )

                    # Use the labeled image for the grid
                    res.images[0] = labeled_image

            except Exception as e:
                errors.display(e, "generating image for xyz plot")
                res = Processed(p, [], p.seed, "")

            # Rest of the original cell function code...
            subgrid_index = 1 + iz
            if grid_infotext[subgrid_index] is None and ix == 0 and iy == 0:
                pc.extra_generation_params = copy(pc.extra_generation_params)
                pc.extra_generation_params["Script"] = self.title()
                if x_opt.label != "Nothing":
                    pc.extra_generation_params["X Type"] = x_opt.label
                    pc.extra_generation_params["X Values"] = x_values
                    if x_opt.label in ["Seed", "Var. seed"] and not no_fixed_seeds:
                        pc.extra_generation_params["Fixed X Values"] = ", ".join(
                            [str(x) for x in xs]
                        )
                if y_opt.label != "Nothing":
                    pc.extra_generation_params["Y Type"] = y_opt.label
                    pc.extra_generation_params["Y Values"] = y_values
                    if y_opt.label in ["Seed", "Var. seed"] and not no_fixed_seeds:
                        pc.extra_generation_params["Fixed Y Values"] = ", ".join(
                            [str(y) for y in ys]
                        )
                grid_infotext[subgrid_index] = processing.create_infotext(
                    pc, pc.all_prompts, pc.all_seeds, pc.all_subseeds
                )

            if grid_infotext[0] is None and ix == 0 and iy == 0 and iz == 0:
                pc.extra_generation_params = copy(pc.extra_generation_params)
                if z_opt.label != "Nothing":
                    pc.extra_generation_params["Z Type"] = z_opt.label
                    pc.extra_generation_params["Z Values"] = z_values
                    if z_opt.label in ["Seed", "Var. seed"] and not no_fixed_seeds:
                        pc.extra_generation_params["Fixed Z Values"] = ", ".join(
                            [str(z) for z in zs]
                        )
                grid_infotext[0] = processing.create_infotext(
                    pc, pc.all_prompts, pc.all_seeds, pc.all_subseeds
                )

            return res

        with SharedSettingsStackHelper():
            if items_per_grid > 0 and not skip_grid:
                items_per_grid = max(1, int(items_per_grid))

                # Determine which axis has the most values
                axis_lengths = {
                    "x": (len(xs), xs, x_opt, "X"),
                    "y": (len(ys), ys, y_opt, "Y"),
                    "z": (len(zs), zs, z_opt, "Z"),
                }

                # Find the axis with the most values
                main_axis = max(axis_lengths.items(), key=lambda x: x[1][0])[0]
                length, values, opt, axis_name = axis_lengths[main_axis]

                if length > 1:  # Only process if we have more than one value
                    chunks = [
                        values[i : i + items_per_grid]
                        for i in range(0, length, items_per_grid)
                    ]
                    all_processed = []

                    for chunk_idx, chunk in enumerate(chunks):
                        print(f"Processing grid {chunk_idx + 1}/{len(chunks)}")

                        grid_args = {
                            "p": p,
                            "xs": chunk if main_axis == "x" else xs,
                            "ys": chunk if main_axis == "y" else ys,
                            "zs": chunk if main_axis == "z" else zs,
                            "x_labels": [
                                x_opt.format_value(p, x_opt, x)
                                for x in (chunk if main_axis == "x" else xs)
                            ],
                            "y_labels": [
                                y_opt.format_value(p, y_opt, y)
                                for y in (chunk if main_axis == "y" else ys)
                            ],
                            "z_labels": [
                                z_opt.format_value(p, z_opt, z)
                                for z in (chunk if main_axis == "z" else zs)
                            ],
                            "cell": cell,
                            "draw_legend": draw_legend,
                            "draw_individual_labels": draw_individual_labels,
                            "include_lone_images": include_lone_images,
                            "include_sub_grids": include_sub_grids,
                            "first_axes_processed": first_axes_processed,
                            "second_axes_processed": second_axes_processed,
                            "margin_size": margin_size,
                        }

                        chunk_processed = draw_xyz_grid(**grid_args)

                        # Keep only necessary data
                        if include_lone_images:
                            z_count = len(grid_args["zs"])
                            main_grid = chunk_processed.images[0]
                            individual_images = chunk_processed.images[z_count + 1 :]
                            chunk_processed.images = [main_grid] + individual_images

                            main_info = chunk_processed.infotexts[0]
                            individual_infos = chunk_processed.infotexts[z_count + 1 :]
                            chunk_processed.infotexts = [main_info] + individual_infos

                            main_prompt = chunk_processed.all_prompts[0]
                            individual_prompts = chunk_processed.all_prompts[
                                z_count + 1 :
                            ]
                            chunk_processed.all_prompts = [
                                main_prompt
                            ] + individual_prompts

                            main_seed = chunk_processed.all_seeds[0]
                            individual_seeds = chunk_processed.all_seeds[z_count + 1 :]
                            chunk_processed.all_seeds = [main_seed] + individual_seeds
                        else:
                            chunk_processed.images = [chunk_processed.images[0]]
                            chunk_processed.all_prompts = [
                                chunk_processed.all_prompts[0]
                            ]
                            chunk_processed.all_seeds = [chunk_processed.all_seeds[0]]
                            chunk_processed.infotexts = [chunk_processed.infotexts[0]]

                        # Save images immediately
                        if opts.grid_save:
                            for i, image in enumerate(chunk_processed.images):
                                suffix = "" if i == 0 else f"_{i}"
                                images.save_image(
                                    image,
                                    p.outpath_grids,
                                    f"xyz_grid_{chunk_idx + 1}{suffix}",
                                    info=chunk_processed.infotexts[i],
                                    extension=opts.grid_format,
                                    prompt=chunk_processed.all_prompts[i],
                                    seed=chunk_processed.all_seeds[i],
                                    grid=True if i == 0 else False,
                                    p=chunk_processed,
                                )

                        # Store only essential information for final results
                        if chunk_idx == 0:
                            final_processed = chunk_processed
                        else:
                            final_processed.images.extend(chunk_processed.images)
                            final_processed.all_prompts.extend(
                                chunk_processed.all_prompts
                            )
                            final_processed.all_seeds.extend(chunk_processed.all_seeds)
                            final_processed.infotexts.extend(chunk_processed.infotexts)

                        # Clear unnecessary references and force garbage collection
                        chunk_processed.images = []
                        chunk_processed.all_prompts = []
                        chunk_processed.all_seeds = []
                        chunk_processed.infotexts = []
                        del chunk_processed
                        gc.collect()

                    return final_processed

            # Handle either skip_grid or normal processing without items_per_grid
            if skip_grid:
                # When skipping grid, process all images individually
                processed = Processed(p, [], p.seed, "")
                processed.images = []
                processed.infotexts = []
                processed.all_prompts = []
                processed.all_seeds = []

                total = len(xs) * len(ys) * len(zs)
                done = 0

                for iz, z in enumerate(zs):
                    for iy, y in enumerate(ys):
                        for ix, x in enumerate(xs):
                            if state.interrupted:
                                break

                            proc = cell(x, y, z, ix, iy, iz)
                            if proc.images:
                                processed.images.extend(proc.images)
                                processed.infotexts.extend(proc.infotexts)
                                processed.all_prompts.extend(proc.all_prompts)
                                processed.all_seeds.extend(proc.all_seeds)

                            done += 1
                            print(f"Processing image {done}/{total}")

                if opts.grid_save:
                    # Save individual images
                    for i, image in enumerate(processed.images):
                        images.save_image(
                            image,
                            p.outpath_grids,
                            f"xyz_image_{i + 1}",
                            info=processed.infotexts[i],
                            extension=opts.grid_format,
                            prompt=processed.all_prompts[i],
                            seed=processed.all_seeds[i],
                            grid=False,
                            p=processed,
                        )

                return processed
            else:
                # Original grid processing without items_per_grid
                processed = draw_xyz_grid(
                    p,
                    xs=xs,
                    ys=ys,
                    zs=zs,
                    x_labels=[x_opt.format_value(p, x_opt, x) for x in xs],
                    y_labels=[y_opt.format_value(p, y_opt, y) for y in ys],
                    z_labels=[z_opt.format_value(p, z_opt, z) for z in zs],
                    cell=cell,
                    draw_legend=draw_legend,
                    draw_individual_labels=draw_individual_labels,
                    include_lone_images=include_lone_images,
                    include_sub_grids=include_sub_grids,
                    first_axes_processed=first_axes_processed,
                    second_axes_processed=second_axes_processed,
                    margin_size=margin_size,
                )

                if not processed.images:
                    # It broke, no further handling needed.
                    return processed

                z_count = len(zs)

                # Set the grid infotexts to the real ones with extra_generation_params
                processed.infotexts[: 1 + z_count] = grid_infotext[: 1 + z_count]

                if opts.grid_save:
                    # Save the main xyz grid
                    images.save_image(
                        processed.images[0],
                        p.outpath_grids,
                        "xyz_grid",
                        info=processed.infotexts[0],
                        extension=opts.grid_format,
                        prompt=processed.all_prompts[0],
                        seed=processed.all_seeds[0],
                        grid=True,
                        p=processed,
                    )

                    # Save sub-grids if enabled
                    if include_sub_grids:
                        for idx in range(1, z_count + 1):
                            images.save_image(
                                processed.images[idx],
                                p.outpath_grids,
                                f"xyz_grid_z_{idx}",
                                info=processed.infotexts[idx],
                                extension=opts.grid_format,
                                prompt=processed.all_prompts[idx],
                                seed=processed.all_seeds[idx],
                                grid=True,
                                p=processed,
                            )

                    # Save individual images if enabled
                    if include_lone_images:
                        individual_images = processed.images[z_count + 1 :]
                        individual_infos = processed.infotexts[z_count + 1 :]
                        individual_prompts = processed.all_prompts[z_count + 1 :]
                        individual_seeds = processed.all_seeds[z_count + 1 :]

                        for idx, (image, info, prompt, seed) in enumerate(
                            zip(
                                individual_images,
                                individual_infos,
                                individual_prompts,
                                individual_seeds,
                            )
                        ):
                            images.save_image(
                                image,
                                p.outpath_grids,
                                f"xyz_grid_image_{idx + 1}",
                                info=info,
                                extension=opts.grid_format,
                                prompt=prompt,
                                seed=seed,
                                grid=False,
                                p=processed,
                            )

                # Organize the final image list
                if include_lone_images:
                    # Keep main grid, sub-grids (if enabled), and individual images
                    main_grid = processed.images[0]
                    sub_grids = (
                        processed.images[1 : z_count + 1] if include_sub_grids else []
                    )
                    individual_images = processed.images[z_count + 1 :]
                    processed.images = [main_grid] + sub_grids + individual_images

                    # Adjust other lists accordingly
                    main_info = processed.infotexts[0]
                    sub_infos = (
                        processed.infotexts[1 : z_count + 1]
                        if include_sub_grids
                        else []
                    )
                    individual_infos = processed.infotexts[z_count + 1 :]
                    processed.infotexts = [main_info] + sub_infos + individual_infos

                    main_prompt = processed.all_prompts[0]
                    sub_prompts = (
                        processed.all_prompts[1 : z_count + 1]
                        if include_sub_grids
                        else []
                    )
                    individual_prompts = processed.all_prompts[z_count + 1 :]
                    processed.all_prompts = (
                        [main_prompt] + sub_prompts + individual_prompts
                    )

                    main_seed = processed.all_seeds[0]
                    sub_seeds = (
                        processed.all_seeds[1 : z_count + 1]
                        if include_sub_grids
                        else []
                    )
                    individual_seeds = processed.all_seeds[z_count + 1 :]
                    processed.all_seeds = [main_seed] + sub_seeds + individual_seeds
                elif include_sub_grids:
                    # Keep only the main grid and sub-grids
                    processed.images = processed.images[: z_count + 1]
                    processed.infotexts = processed.infotexts[: z_count + 1]
                    processed.all_prompts = processed.all_prompts[: z_count + 1]
                    processed.all_seeds = processed.all_seeds[: z_count + 1]
                else:
                    # Keep only the main grid
                    processed.images = [processed.images[0]]
                    processed.infotexts = [processed.infotexts[0]]
                    processed.all_prompts = [processed.all_prompts[0]]
                    processed.all_seeds = [processed.all_seeds[0]]

                return processed
