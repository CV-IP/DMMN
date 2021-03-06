#  #!/usr/bin/env python
#   Copyright (c) 2019. ShiJie Sun at the Chang'an University
#   This work is licensed under the terms of the Creative Commons Attribution-NonCommercial-ShareAlike 3.0 License.
#   For a copy, see <http://creativecommons.org/licenses/by-nc-sa/3.0/>.
#   Author: shijie Sun
#   Email: shijieSun@chd.edu.cn
#   Github: www.github.com/shijieS
#

import os
import torch
from torch.autograd import Variable
from layers.dmmn import DMMN
import cv2
import numpy as np
from draw_utils.DrawBoxes import DrawBoxes
from scipy.optimize import linear_sum_assignment
from layers.dmmn.utils.box_utils import jaccard
import time
# from concurrent.futures import ThreadPoolExecutor, wait, ALL_COMPLETED
from deco import concurrent, synchronized


class Config:
    tracker_name = "DMMN"
    tracker_version = "V1"
    cuda = True
    weight_file = "./weights/ssdt_cvpr19.pth"
    frame_num = 16
    frame_height = 168
    frame_width = 168
    pixel_mean = [104, 117, 123]
    frame_scale = 2
    share_frame_num = 5
    detect_conf_thresh = 0.5
    show_result = True
    category_map = {
        1: "Sedan",
        2: "Suv",
        3: "Taxi",
        4: "Van",
        5: "Bus",
        6: "MiniVan",
        7: "Truck-Box-Large",
        8: "Truck-Pickup",
        9: "Hatchback",
        10: "Police",
        11: "Truck-Box-Med",
        12: "Truck-Util",
        13: "Truck-Flatbed"
    }
    max_age = 16
    max_direction_thresh = 3.14 / 2.0
    min_similarity = 0.3
    min_visibility = 0.5
    save_images = True
    save_images_folder = "./"
    save_track_data = True

    @staticmethod
    def init(name, version, config,
             show_result=True,
             share_frame_num=5, max_age=16,
             max_direction_thresh=1.57,
             min_similarity=0.01, min_visibility=0.5,
             max_thread_num=32):
        Config.tracker_name = name
        Config.tracker_version = version
        Config.cuda = config["cuda"]
        Config.weight_file = config["test"]["resume"]
        Config.frame_num = config["frame_max_input_num"]
        Config.frame_height = config["frame_size"]
        Config.frame_width = config["frame_size"]
        Config.pixel_mean = config["pixel_mean"]
        Config.frame_scale = config["frame_sample_scale"]
        Config.share_frame_num = share_frame_num
        Config.detect_conf_thresh = config["test"]["detect_conf_thresh"]
        Config.show_result = show_result
        Config.category_map = {v:k for k, v in reversed(tuple(config["replace_map"].items())) if v != -1}
        Config.max_age = max_age
        Config.max_direction_thresh = max_direction_thresh
        Config.min_similarity = min_similarity
        if 'tracker_min_iou_thresh' in config['test'].keys():
            Config.min_similarity = config['test']['tracker_min_iou_thresh']

        Config.min_visibility = min_visibility
        if 'tracker_min_visibility' in config['test'].keys():
            Config.min_visibility = config['test']['tracker_min_visibility']

        Config.max_thread_num = max_thread_num
        Config.save_images = config["test"]["debug_save_image"]
        Config.save_images_folder = config["test"]["image_save_folder"]
        Config.save_track_data = config["test"]["save_track_data"]

    @staticmethod
    def set_image_folder(image_folder):
        if not os.path.exists(image_folder):
            os.makedirs(image_folder)
        Config.save_images_folder = image_folder

class Node:
    global_id = 1

    def __init__(self, times, frame_indexes, params, boxes, p_c, p_e, category, width, height):

        self.times = times
        self.frame_indexes = frame_indexes
        self.params = params
        self.boxes =boxes
        self.p_c = p_c
        self.p_e = p_e
        self.id = Node.global_id
        self.category = category
        Node.global_id += 1
        self.width = width
        self.height = height

    def draw(self, frames, track_id):
        # print(self.category)
        DrawBoxes.draw_node_result(frames, self.boxes, self.p_c, self.p_e,
                                   Config.category_map[self.category], track_id)
        return frames

    def save_mot_data(self, file_name, track_id):
        frame_num = len(self.frame_indexes)
        if Config.cuda:
            boxes = self.boxes.data.cpu().numpy()
            p_e = self.p_e.data.cpu().numpy()
        else:
            boxes = self.boxes.data.numpy()
            p_e = self.p_e.data.numpy()

        boxes *= np.array([self.width, self.height, self.width, self.height])

        saved_data = np.array([[f]+[track_id]+b.tolist()+[float(self.p_c)]+[self.category]+[e]
                      for i, b, e, f in zip(range(frame_num-Config.share_frame_num), boxes, p_e, self.frame_indexes)])
        # saved_data[:, [6, 7]] = -1
        saved_data[:, [4, 5]] -=  saved_data[:, [2, 3]]
        # saved_data = saved_data[:, [0, 1, 2, 3, 4, 5, 8, 6, 7]]
        with open(file_name, "a+") as f:
            np.savetxt(f, saved_data, fmt=["%d", "%d", "%f", "%f", "%f", "%f", "%f", "%d", "%f"],
                       delimiter=",")

class Track:
    global_id = 1

    def __init__(self, node):
        self.nodes = [node]
        self.id = Track.global_id
        Track.global_id += 1
        self.age = 0
        self.is_valid = True

    def update(self, node):
        self.nodes += [node]
        self.age = 0

    def get_similarity(self, node):
        my_node = self.nodes[-1]

        # 0. if they are in different category, then return 0
        if node.category != my_node.category or node.frame_indexes[0] not in my_node.frame_indexes.tolist():
            return 0

        # 1. select intersect frame indexes
        frame_index_start = my_node.frame_indexes.tolist().index(node.frame_indexes[0])
        frame_index_end = len(my_node.frame_indexes)

        # 2. get the iou
        ious = jaccard(my_node.boxes[frame_index_start:frame_index_end, :], node.boxes[0:frame_index_end-frame_index_start, :])

        # 3. get the mask
        visibility_mask = node.p_e[0:frame_index_end-frame_index_start] >= Config.min_visibility

        # 4. get the best similarity
        if visibility_mask.sum() == 0:
            return 0
        else:
            return ious.diag()[visibility_mask].sum()


    def get_direction_distance(self, node):
        return 0

    def draw(self, frames):
        if self.age != 1:
            return frames

        self.nodes[-1].draw(frames, self.id)

        return frames

    def save_mot_data(self, file_name):
        for n in self.nodes:
            # print(n.frame_indexes)
            n.save_mot_data(file_name, self.id)


class TrackSet:
    def __init__(self):
        self.tracks = []
        self.removed_tracks = []

    @concurrent
    @staticmethod
    def _get_tracks_similarity_direction(i, n, tracks):
        similarity = []
        direction = []
        for j, t in enumerate(tracks):
            similarity += [t.get_similarity(n)]
            direction += [t.get_direction_distance(n)]
        return similarity, direction

    @synchronized
    @staticmethod
    def _get_similarity_direction(nodes, tracks):
        similarity = []
        direction = []
        for i, n in enumerate(nodes):
            s, d = TrackSet._get_tracks_similarity_direction(i, n, tracks)
            similarity += [s]
            direction += [d]

        return similarity, direction


    def is_match(self, nodes):
        # implement serialized
        similarity = torch.zeros((len(nodes), len(self.tracks)))
        direction = torch.zeros((len(nodes), len(self.tracks)))

        for i, n in enumerate(nodes):
            for j, t in enumerate(self.tracks):
                similarity[i, j] += t.get_similarity(n)
                direction[i, j] += t.get_direction_distance(n)

        # similarity, direction = TrackSet._get_similarity_direction(nodes, self.tracks)
        # similarity = torch.from_numpy(np.array(similarity))
        # direction = torch.from_numpy(np.array(direction))

        direction_mask = direction >= Config.max_direction_thresh
        similarity_mask = similarity < Config.min_similarity

        mask = (direction_mask + similarity_mask) > 0
        similarity[mask] = 0

        # convert to numpy
        similarity = similarity.data.cpu().numpy()

        # hungarian algorithm

        start_time = time.time()
        row_indexes, column_indexes = linear_sum_assignment(-similarity)
        print("linear_sum_assigment time(ms):{}".format(32/(time.time() - start_time)))

        # create the map between node and the track

        node_indexes = np.zeros(len(nodes), dtype=int) - 1
        node_indexes[row_indexes] = column_indexes
        node_indexes[similarity.sum(axis=1) == 0] = -1

        return node_indexes


    def update(self, nodes):
        #1. tracks is empty (first frame or some case else)
        if len(self.tracks) == 0:
            for node in nodes:
                self.tracks += [Track(node)]

        elif len(nodes) > 0:
            #2. decide wether to create a new track or not
            node_indexes = self.is_match(nodes)
            for i in range(len(nodes)):
                if node_indexes[i] == -1:
                    self.tracks += [Track(nodes[i])]
                else:
                    self.tracks[node_indexes[i]].update(nodes[i])

        #3. add ages
        for t in self.tracks:
            t.age += 1

        #4. remove old tracks
        if Config.save_track_data:
            self.removed_tracks = [t for t in self.tracks if t.age >= 2]

        self.tracks = [t for t in self.tracks if t.age < 2]


    def draw(self, frames):
        for t in self.tracks:
            t.draw(frames)

        return frames

    def save_mot_data(self, file_name, force=False):
        if force:
            self.removed_tracks = [t for t in self.tracks]
            Node.global_id = 1
            Track.global_id = 1
            self.tracks = []

        for t in self.removed_tracks:
            t.save_mot_data(file_name)
        self.removed_tracks = []

class Tracker:
    def __init__(self, name, version, config=None):
        self.all_fps = []
        if config is not None:
            Config.init(name, version, config)

        if Config.save_images:
            if not os.path.exists(Config.save_images_folder):
                os.makedirs(Config.save_images_folder)

        #0. set torch cuda configure
        if torch.cuda.is_available():
            if Config.cuda:
                torch.set_default_tensor_type('torch.cuda.FloatTensor')
            if not Config.cuda:
                print("WARNING: It looks like you have a CUDA device, but aren't " +
                      "using CUDA.\nRun with --cuda for optimal training speed.")
                torch.set_default_tensor_type('torch.FloatTensor')
        else:
            torch.set_default_tensor_type('torch.FloatTensor')

        #1. create a DMMN
        self.dmmn = DMMN.build("test")
        self.net = self.dmmn
        if Config.cuda:
            self.net = self.net.cuda()
            self.net = torch.nn.DataParallel(self.dmmn)

        #2. load weight
        if not os.path.exists(Config.weight_file):
            raise FileNotFoundError("cannot find {}".format(Config.weight_file))
        else:
            print("Loading the network")
            self.dmmn.load_weights(Config.weight_file)

        self.net.eval()

        #3. create a track set
        self.tracks = TrackSet()
        self.save_frame_index = 0

    def save_mot_result(self, file_name, force=False):
        if not Config.save_track_data:
            return None

        self.tracks.save_mot_data(file_name, force)


    def update(self, frames, times, frame_index):
        """
        frames is a list of opencv images
        :param input_frames: a list of opencv images
        :param input_times: a list of times
        :return: None
        """

        # 1. format frames and times
        if frames is None or times is None:
            return

        h, w, _ = frames[0].shape
        frame_indexes = np.arange(frame_index, frame_index+Config.frame_num*Config.frame_scale)
        selected_indexes = np.arange(0, Config.frame_num) * Config.frame_scale
        input_frames = [frames[i] for i in selected_indexes]
        input_times = times

        if len(input_frames) != Config.frame_num:
            raise AssertionError("number of frames or times should be {}".format(Config.frame_num))

        input_frames = [torch.from_numpy(cv2.resize(f, (Config.frame_width, Config.frame_height)).astype(np.float32) - Config.pixel_mean)
                        for f in input_frames]

        input_times = torch.from_numpy(np.array(input_times))[None, :]
        frames_input = torch.stack(input_frames, dim=0)[None, :].permute(0, 4, 1, 2, 3)
        with torch.no_grad():
            if Config.cuda:
                frames_input = Variable(frames_input.float().cuda())
                times_input = Variable(input_times.float().cuda())
            else:
                frames_input = Variable(frames_input.float())
                times_input = Variable(input_times.float())

        # 2. get the image results
        start_time = time.time()
        output_params, output_p_c, output_p_e, output_boxes = self.net(frames_input, times_input)
        fps = (len(frames)-Config.share_frame_num) / (time.time() - start_time)

        self.all_fps += [fps]
        mean_fps = np.mean(self.all_fps)
        print("fps is {} f/s".format(fps))
        print("mean fps is {} f/s".format(mean_fps))


        # 3. update recorder
        # output_p_c_mask = output_p_c > Config.detect_conf_thresh
        output_p_e = output_p_e.permute(0, 1, 3, 2)
        output_p_c = output_p_c.permute(0, 2, 1)
        output_boxes = output_boxes.permute(0, 1, 3, 2, 4)

        nodes = []
        for c in range(1, output_p_c.size(2)):
            mask = output_p_c[0, :, c] > Config.detect_conf_thresh
            if mask.sum() == 0:
                continue

            boxes = output_boxes[0, c, mask, :, :]
            p_c = output_p_c[0, mask, c]
            p_e = output_p_e[0, 1, mask, :]
            param = output_params[0, c, mask, :, :]

            nodes += [Node(times_input, frame_indexes, param[i, :], boxes[i, :], p_c[i], p_e[i, :], c, w, h)
                      for i in range(p_c.shape[0])]

            # if Config.show_result:
            #     DrawBoxes.draw_dmmn_result(frames, boxes, p_c, p_e, category=Config.category_map[c])

        self.tracks.update(nodes)
        self.tracks.draw(frames)

        if Config.show_result:
            for frame, index in zip(frames, frame_indexes):
                cv2.imshow("result", frame)
                cv2.waitKey(20)
                if Config.save_images_folder:
                    cv2.imwrite(os.path.join(Config.save_images_folder, "{0:08}.jpg".format(index)),
                                frame)
                # cv2.imwrite("result/{0:08}.png".format(self.save_frame_index), frame)
                self.save_frame_index += 1

            pass

