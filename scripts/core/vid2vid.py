import sys, os
basedirs = [os.getcwd()]

for basedir in basedirs:
    paths_to_ensure = [
        basedir,
        basedir + '/extensions/sd-cn-animation/scripts',
        basedir + '/extensions/SD-CN-Animation/scripts'
        ]

    for scripts_path_fix in paths_to_ensure:
        if not scripts_path_fix in sys.path:
            sys.path.extend([scripts_path_fix])

import math
import os
import sys
import traceback

import numpy as np
from PIL import Image

from modules import devices, sd_samplers
from modules import shared, sd_hijack, lowvram

from modules.shared import devices
import modules.shared as shared

import gc
import cv2
import gradio as gr

import time
import skimage
import datetime

from core.flow_utils import RAFT_estimate_flow, RAFT_clear_memory, compute_diff_map
from core import utils

class sdcn_anim_tmp:
  prepear_counter = 0
  process_counter = 0
  input_video = None
  output_video = None
  curr_frame = None
  prev_frame = None
  prev_frame_styled = None
  prev_frame_alpha_mask = None
  fps = None
  total_frames = None
  prepared_frames = None
  prepared_next_flows = None
  prepared_prev_flows = None
  frames_prepared = False

def read_frame_from_video():
  # Reading video file
  if sdcn_anim_tmp.input_video.isOpened():
    ret, cur_frame = sdcn_anim_tmp.input_video.read()
    if cur_frame is not None: 
      cur_frame = cv2.cvtColor(cur_frame, cv2.COLOR_BGR2RGB) 
  else:
    cur_frame = None
    sdcn_anim_tmp.input_video.release()
  
  return cur_frame

def get_cur_stat():
  stat =  f'Frames prepared: {sdcn_anim_tmp.prepear_counter + 1} / {sdcn_anim_tmp.total_frames}; '
  stat += f'Frames processed: {sdcn_anim_tmp.process_counter + 1} / {sdcn_anim_tmp.total_frames}; '
  return stat

def clear_memory_from_sd():
  if shared.sd_model is not None:
    sd_hijack.model_hijack.undo_hijack(shared.sd_model)
    try:
      lowvram.send_everything_to_cpu()
    except Exception as e:
      ...
    del shared.sd_model
    shared.sd_model = None
  gc.collect()
  devices.torch_gc()

def start_process(*args):
  args_dict = utils.args_to_dict(*args)
  args_dict = utils.get_mode_args('v2v', args_dict)
  
  sdcn_anim_tmp.process_counter = 0
  sdcn_anim_tmp.prepear_counter = 0

  # Open the input video file
  sdcn_anim_tmp.input_video = cv2.VideoCapture(args_dict['file'].name)
  
  # Get useful info from the source video
  sdcn_anim_tmp.fps = int(sdcn_anim_tmp.input_video.get(cv2.CAP_PROP_FPS))
  sdcn_anim_tmp.total_frames = int(sdcn_anim_tmp.input_video.get(cv2.CAP_PROP_FRAME_COUNT))

  # Create an output video file with the same fps, width, and height as the input video
  output_video_name = f'outputs/sd-cn-animation/vid2vid/{datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}.mp4'
  os.makedirs(os.path.dirname(output_video_name), exist_ok=True)
  sdcn_anim_tmp.output_video = cv2.VideoWriter(output_video_name, cv2.VideoWriter_fourcc(*'mp4v'), sdcn_anim_tmp.fps, (args_dict['width'], args_dict['height']))

  curr_frame = read_frame_from_video()
  curr_frame = cv2.resize(curr_frame, (args_dict['width'], args_dict['height']))
  sdcn_anim_tmp.prepared_frames = np.zeros((11, args_dict['height'], args_dict['width'], 3), dtype=np.uint8)
  sdcn_anim_tmp.prepared_next_flows = np.zeros((10, args_dict['height'], args_dict['width'], 2))
  sdcn_anim_tmp.prepared_prev_flows = np.zeros((10, args_dict['height'], args_dict['width'], 2))
  sdcn_anim_tmp.prepared_frames[0] = curr_frame

  args_dict['init_img'] = Image.fromarray(curr_frame)
  utils.set_CNs_input_image(args_dict, Image.fromarray(curr_frame))
  processed_frames, _, _, _ = utils.img2img(args_dict)
  processed_frame = np.array(processed_frames[0])
  processed_frame = skimage.exposure.match_histograms(processed_frame, curr_frame, multichannel=False, channel_axis=-1)
  processed_frame = np.clip(processed_frame, 0, 255).astype(np.uint8)
  #print('Processed frame ', 0)

  sdcn_anim_tmp.curr_frame = curr_frame
  sdcn_anim_tmp.prev_frame = curr_frame.copy()
  sdcn_anim_tmp.prev_frame_styled = processed_frame.copy()
  utils.shared.is_interrupted = False
  yield get_cur_stat(), sdcn_anim_tmp.curr_frame, None, None, processed_frame, '', gr.Button.update(interactive=False), gr.Button.update(interactive=True)

  try:
    for step in range((sdcn_anim_tmp.total_frames-1) * 2):
      if utils.shared.is_interrupted: break
      
      args_dict = utils.args_to_dict(*args)
      args_dict = utils.get_mode_args('v2v', args_dict)

      occlusion_mask = None
      prev_frame = None
      curr_frame = sdcn_anim_tmp.curr_frame
      warped_styled_frame_ = gr.Image.update()
      processed_frame = gr.Image.update()

      prepare_steps = 10
      if sdcn_anim_tmp.process_counter % prepare_steps == 0 and not sdcn_anim_tmp.frames_prepared: # prepare next 10 frames for processing
          #clear_memory_from_sd()
          device = devices.get_optimal_device()

          curr_frame = read_frame_from_video()
          if curr_frame is not None: 
              curr_frame = cv2.resize(curr_frame, (args_dict['width'], args_dict['height']))
              prev_frame = sdcn_anim_tmp.prev_frame.copy()

              next_flow, prev_flow, occlusion_mask, frame1_bg_removed, frame2_bg_removed = RAFT_estimate_flow(prev_frame, curr_frame, subtract_background=False, device=device)
              occlusion_mask = np.clip(occlusion_mask * 0.1 * 255, 0, 255).astype(np.uint8)

              cn = sdcn_anim_tmp.prepear_counter % 10
              if sdcn_anim_tmp.prepear_counter % 10 == 0:
                  sdcn_anim_tmp.prepared_frames[cn] = sdcn_anim_tmp.prev_frame
              sdcn_anim_tmp.prepared_frames[cn + 1] = curr_frame.copy()
              sdcn_anim_tmp.prepared_next_flows[cn] = next_flow.copy()
              sdcn_anim_tmp.prepared_prev_flows[cn] = prev_flow.copy()
              #print('Prepared frame ', cn+1)

              sdcn_anim_tmp.prev_frame = curr_frame.copy()

          sdcn_anim_tmp.prepear_counter += 1
          if sdcn_anim_tmp.prepear_counter % prepare_steps == 0 or \
          sdcn_anim_tmp.prepear_counter >= sdcn_anim_tmp.total_frames - 1 or \
          curr_frame is None:
              # Remove RAFT from memory
              RAFT_clear_memory()
              sdcn_anim_tmp.frames_prepared = True
      else:
          # process frame
          sdcn_anim_tmp.frames_prepared = False

          cn = sdcn_anim_tmp.process_counter % 10 
          curr_frame = sdcn_anim_tmp.prepared_frames[cn+1]
          prev_frame = sdcn_anim_tmp.prepared_frames[cn]
          next_flow = sdcn_anim_tmp.prepared_next_flows[cn]
          prev_flow = sdcn_anim_tmp.prepared_prev_flows[cn]

          # process current frame
          args_dict['init_img'] = Image.fromarray(curr_frame)
          args_dict['seed'] = -1
          utils.set_CNs_input_image(args_dict, Image.fromarray(curr_frame))
          processed_frames, _, _, _ = utils.img2img(args_dict)
          processed_frame = np.array(processed_frames[0])


          alpha_mask, warped_styled_frame = compute_diff_map(next_flow, prev_flow, prev_frame, curr_frame, sdcn_anim_tmp.prev_frame_styled)
          warped_styled_frame_ = warped_styled_frame.copy()

          if sdcn_anim_tmp.process_counter > 0:
              alpha_mask = alpha_mask + sdcn_anim_tmp.prev_frame_alpha_mask * 0.5
          sdcn_anim_tmp.prev_frame_alpha_mask = alpha_mask
          # alpha_mask = np.clip(alpha_mask + 0.05, 0.05, 0.95)
          alpha_mask = np.clip(alpha_mask, 0, 1)

          fl_w, fl_h = prev_flow.shape[:2]
          prev_flow_n = prev_flow / np.array([fl_h,fl_w])
          flow_mask = np.clip(1 - np.linalg.norm(prev_flow_n, axis=-1)[...,None], 0, 1)

          # fix warped styled frame from duplicated that occures on the places where flow is zero, but only because there is no place to get the color from
          warped_styled_frame = curr_frame.astype(float) * alpha_mask * flow_mask + warped_styled_frame.astype(float) * (1 - alpha_mask * flow_mask)
          
          # This clipping at lower side required to fix small trailing issues that for some reason left outside of the bright part of the mask, 
          # and at the higher part it making parts changed strongly to do it with less flickering. 
          
          occlusion_mask = np.clip(alpha_mask * 255, 0, 255).astype(np.uint8)

          # normalizing the colors
          processed_frame = skimage.exposure.match_histograms(processed_frame, curr_frame, multichannel=False, channel_axis=-1)
          processed_frame = processed_frame.astype(float) * alpha_mask + warped_styled_frame.astype(float) * (1 - alpha_mask)
          
          processed_frame = processed_frame * 0.9 + curr_frame * 0.1
          processed_frame = np.clip(processed_frame, 0, 255).astype(np.uint8)
          sdcn_anim_tmp.prev_frame_styled = processed_frame.copy()

          args_dict['init_img'] = Image.fromarray(processed_frame)
          args_dict['denoising_strength'] = args_dict['fix_frame_strength']
          args_dict['seed'] = 8888
          utils.set_CNs_input_image(args_dict, Image.fromarray(curr_frame))
          processed_frames, _, _, _ = utils.img2img(args_dict)
          processed_frame = np.array(processed_frames[0])

          processed_frame = np.clip(processed_frame, 0, 255).astype(np.uint8)
          warped_styled_frame_ = np.clip(warped_styled_frame_, 0, 255).astype(np.uint8)
          

          # Write the frame to the output video
          frame_out = np.clip(processed_frame, 0, 255).astype(np.uint8)
          frame_out = cv2.cvtColor(frame_out, cv2.COLOR_RGB2BGR) 
          sdcn_anim_tmp.output_video.write(frame_out)

          sdcn_anim_tmp.process_counter += 1
          if sdcn_anim_tmp.process_counter >= sdcn_anim_tmp.total_frames - 1:
              sdcn_anim_tmp.input_video.release()
              sdcn_anim_tmp.output_video.release()
              sdcn_anim_tmp.prev_frame = None

      #print(f'\nEND OF STEP {step}, {sdcn_anim_tmp.prepear_counter}, {sdcn_anim_tmp.process_counter}')
      yield get_cur_stat(), curr_frame, occlusion_mask, warped_styled_frame_, processed_frame, '', gr.Button.update(interactive=False), gr.Button.update(interactive=True)
  except:
    pass

  RAFT_clear_memory()

  sdcn_anim_tmp.input_video.release()
  sdcn_anim_tmp.output_video.release()

  curr_frame = gr.Image.update()
  occlusion_mask = gr.Image.update()
  warped_styled_frame_ = gr.Image.update() 
  processed_frame = gr.Image.update()

  yield get_cur_stat(), curr_frame, occlusion_mask, warped_styled_frame_, processed_frame, '', gr.Button.update(interactive=True), gr.Button.update(interactive=False)