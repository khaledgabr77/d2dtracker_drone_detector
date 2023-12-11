#!/usr/bin/env python3

"""
@Description

This node receives 
    vision_msgs/msg/Detection2dArray msg
    Depth image msg sensor_msgs/msg/Image
and converts the 2D Yolo detection to a 3D position as
    geometry_msgs/msg/PoseArray

Author: Mohamed Abdelkader
Contact: mohamedashraf123@gmail.com

"""

import cv2
import numpy as np
import math
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image, CameraInfo
from cv_bridge import CvBridge
# from vision_msgs.msg import Detection2DArray, Detection2D
from yolov8_msgs.msg import Detection
from yolov8_msgs.msg import DetectionArray

from tf2_ros import TransformException
from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener
from tf2_geometry_msgs import Pose as TF2Pose

from geometry_msgs.msg import PoseArray, PointStamped, Pose, TransformStamped, PoseWithCovarianceStamped
from tf2_geometry_msgs import do_transform_point, do_transform_pose, do_transform_pose_with_covariance_stamped
from multi_target_kf.msg import KFTracks
import visualization_msgs.msg
from visualization_msgs.msg import Marker
import numpy as np
from geometry_msgs.msg import Pose, Point
from tf2_ros import LookupException, ConnectivityException, ExtrapolationException
from std_msgs.msg import Header
from rclpy.node import Node
import tf2_ros
from geometry_msgs.msg import Pose, Point, Quaternion
import copy
from std_msgs.msg import Float64
class Yolo2PoseNode(Node):

    def __init__(self):
        # Initiate the node
        super().__init__("yolo2pose_node")
        # Declare parameters
        self.declare_parameters(
            namespace='',
            parameters=[
                ('debug', True),
                ('publish_processed_images', True),
                ('reference_frame', 'map'),
                ('camera_frame', 'x500_d435_1/link/realsense_d435'),
            ]
        )
        self.latest_pixels_ = []
        self.latest_covariances_2d_ = []
        self.latest_depth_ranges_= []

        self.track_data = []
        self.pose_data = []
        self.msg_count = 0
        self.msg_limit = 100
        # Get parameters
        self.debug_ = self.get_parameter('debug').get_parameter_value().bool_value
        self.publish_processed_images_ = self.get_parameter('publish_processed_images').get_parameter_value().bool_value
        self.reference_frame_ = self.get_parameter('reference_frame').get_parameter_value().string_value
        self.camera_frame_ = self.get_parameter('camera_frame').get_parameter_value().string_value

        self.latest_kf_tracks_msg_ = KFTracks()

        self.cv_bridge_ = CvBridge()

        # Camera intrinsics
        self.camera_info_ = None
        self.yolo_detections_msg_ = DetectionArray()
        self.detection_pose_msg_ = PoseArray()
        self.kalman_filter_pose_msg_ = KFTracks()
        self.imag_ = Image()
        # Last detection time stamp in seconds
        self.last_detection_t_ = 0.0
        self.last_kf_measurments_t_ = 0.0
        # self.depth_roi_ = 5
        # self.depth_range_ = 5
        # Ref: https://docs.ros.org/en/humble/Tutorials/Intermediate/Tf2/Writing-A-Tf2-Listener-Py.html
        self.tf_buffer_ = Buffer()
        self.tf_listener_ = TransformListener(self.tf_buffer_,self)

        # Subscribers
        # self.image_sub_ = self.create_subscription(Image,"interceptor/depth_image",self.depthCallback, qos_profile_sensor_data)
        self.image_sub_ = self.create_subscription(Image,"interceptor/depth_image",self.depthCallback, 10)
        self.caminfo_sub_ = self.create_subscription(CameraInfo, 'interceptor/camera_info', self.caminfoCallback, 10)
        self.detections_sub_ = self.create_subscription(DetectionArray, 'detections', self.detectionsCallback, 10)
        self.kalman_filter_pose_ = self.create_subscription(KFTracks, 'kf/good_tracks', self.handle_KF_tracker_data, 10)
        #self.dbg_image_sub_ = self.create_subscription(Image, "interceptor/depth_image", self.dbg_image_callback, 10)

        # Publishers
        self.poses_pub_ = self.create_publisher(PoseArray,'yolo_poses',10)
        # self.pose_kf_meas_pub_ = self.create_publisher(PoseArray, "kf_poses_mes", 10)
        self.overlay_ellipses_image_yolo_ = self.create_publisher(Image, "overlay_yolo_image", 10)
        self.overlay_ellipses_image_kf_ = self.create_publisher(Image, "overlay_kf_image", 10)
        self.pose_publisher = self.create_publisher(Pose, 'transformed_pose', 10)
        self.timer = self.create_timer(0.1, self.publish_transformed_pose)
        self.mse_publisher = self.create_publisher(Float64, 'mse', 10)
        self.rmse_publisher = self.create_publisher(Float64, 'rmse', 10)
        self.abs_publisher = self.create_publisher(Float64, 'abs', 10)

        timer_period = 1  # seconds
        self.timer = self.create_timer(timer_period, self.calculate_errors)

        self.declare_parameter('use_yolo', True)
        self.declare_parameter('use_kf', True)
        self.declare_parameter('depth_roi', 5.0)
        self.declare_parameter('std_range', 5.0)


    
    def collect_data(self, msg:KFTracks, actual_pose_values):
            
        # Collect track data
        for track in msg.tracks:
            x = track.pose.pose.position.x
            y = track.pose.pose.position.y
            z = track.pose.pose.position.z

            self.track_data.append((x, y, z)) 
                
            self.get_logger().info(f'TrackData: {len(self.track_data)}')

            # Collect pose data
            # pose_tr = self.actual_pose()
            pose_x, pose_y, pose_z = actual_pose_values

            self.pose_data.append((pose_x, pose_y, pose_z))
            self.get_logger().info(f'PoseData: {len(self.pose_data)}')

            # Increment message count
            self.msg_count += 1
            # print(f"Count: {self.msg_count}")

            # Check if reached the limit
            if self.msg_count >= self.msg_limit:
                self.calculate_errors()
                self.reset_data()

    def calculate_errors(self):
        if len(self.pose_data) < self.msg_limit or len(self.track_data) < self.msg_limit:
            # Insufficient data to calculate errors
            print("Insufficient data for calculations")
            return
    
        squared_error = 0.0
        abs_error = 0.0
        for i in range(self.msg_limit):
            pose_x, pose_y, pose_z = self.pose_data[i]
            x, y, z = self.track_data[i]
            # clacutale Mean squared Error 
            kf_err_x = ((pose_x - x) **2)
            kf_err_y = ((pose_y - y) **2)
            kf_err_z = ((pose_z - z) **2)
            # print(f"kf_err_x: {kf_err_x}")

            # clacutale Absolute Position Error
            abs_err_x = abs(pose_x - x)
            abs_err_y = abs(pose_y - y)
            abs_err_z = abs(pose_z - z)

            squared_error += kf_err_x + kf_err_y + kf_err_z
            abs_error +=  abs_err_x + abs_err_y + abs_err_z
        # Calculate Mean Squared Error
        # num_data_points = self.msg_limit * 3  # Three dimensions (x, y, z) per data point
        mse = squared_error / self.msg_limit
        rmse = np.sqrt(mse)
        avg_abs_error = abs_error

        # Publish calculated errors
        self.publish_mse(mse)
        self.publish_rmse(rmse)
        self.publish_abs(avg_abs_error)
        # print(f"Total KF Error: {squared_error}")
        # print(f"Mean Squared Error: {mse}")
        # self.get_logger().info(f'Mean Squared Error: {mse}')
        # self.get_logger().info(f'Root Mean Squared Error: {rmse}')
        # self.get_logger().info(f'Absolute Position Error: {avg_abs_error}')
    def reset_data(self):
        # Clear data and reset message count
        self.track_data = []
        self.pose_data = []
        self.msg_count = 0

    def publish_mse(self, mse):
        msg = Float64()
        msg.data = mse
        self.mse_publisher.publish(msg)

    def publish_rmse(self, rmse):
        msg = Float64()
        msg.data = rmse
        self.rmse_publisher.publish(msg)

    def publish_abs(self, avg_abs_error):
        msg = Float64()
        msg.data = avg_abs_error
        self.abs_publisher.publish(msg)

    def depthCallback(self, msg: Image):
        use_yolo = self.get_parameter('use_yolo').value
        use_kf = self.get_parameter('use_kf').value

        kf_msg = copy.deepcopy(self.latest_kf_tracks_msg_)
        yolo_msg = copy.deepcopy(self.yolo_detections_msg_)

        poses_msg = PoseArray()
        poses_msg.header = copy.deepcopy(msg.header)
        poses_msg.header.frame_id = self.reference_frame_

        new_measurements_yolo = False
        new_measurements_kf = False


        current_detection_t = float(yolo_msg.header.stamp.sec) + \
                                float(yolo_msg.header.stamp.nanosec)/1e9

        if len(yolo_msg.detections) > 0:

            if current_detection_t > self.last_detection_t_:
                new_measurements_yolo = True
                self.last_detection_t_ = current_detection_t


        current_kf_measurment_t = float(kf_msg.header.stamp.sec) + \
                                float(kf_msg.header.stamp.nanosec)/1e9
        if len(kf_msg.tracks) > 0:
            if current_kf_measurment_t > self.last_kf_measurments_t_ and not new_measurements_yolo:
                new_measurements_kf = True
                self.last_kf_measurments_t_ = current_kf_measurment_t


        
        if use_yolo and new_measurements_yolo:
            yolo_poses = self.yolo_process_pose(copy.deepcopy(msg))
            if len(yolo_poses.poses) > 0:

                self.poses_pub_.publish(yolo_poses)

        else:
            if use_kf:
                if new_measurements_kf:
                    kf_poses = self.kf_process_pose(copy.deepcopy(msg))
                    if len(kf_poses.poses) > 0:

                        self.poses_pub_.publish(kf_poses)
    
    def yolo_process_pose(self, msg: Image):
        yolo_msg = copy.deepcopy(self.yolo_detections_msg_)

        if self.camera_info_ is None:
            if(self.debug_):
                self.get_logger().warn("[Yolo2PoseNode::yolo_process_pose] camera_info is None. Return")
            return

        try:
            # Convert ROS Image message to OpenCV image
            cv_image = self.cv_bridge_.imgmsg_to_cv2(msg , desired_encoding="32FC1")#"16UC1")
        except Exception as e:
            self.get_logger().error("[Yolo2PoseNode::yolo_process_pose] Image to CvImg conversion error {}".format(e))
            return
        try:
            transform = self.tf_buffer_.lookup_transform(
                self.reference_frame_,
                msg.header.frame_id,
                rclpy.time.Time(),
                timeout=rclpy.duration.Duration(seconds=1.0))
        except TransformException as ex:
            self.get_logger().error(
                f'[Yolo2PoseNode::depthCallback] Could not transform {self.reference_frame_} to {msg.header.frame_id}: {ex}')
            return

        #depth_image_type = cv_image.dtype
        #print("Depth Image Type:", depth_image_type)
        obj = Detection()
        poses_msg = PoseArray()
        poses_msg.header = copy.deepcopy(yolo_msg.header)
        poses_msg.header.frame_id = self.reference_frame_
        transformed_pose_msg = Pose()
        poses_msg.poses.clear()
        depth_at_centroid = 0.0
        ellipse_color = (0, 255, 0)  
        text_color = (0, 255, 0)  
        for obj in yolo_msg.detections:
            x = int(obj.bbox.center.position.x - obj.bbox.size.x/2)
            y = int(obj.bbox.center.position.y - obj.bbox.size.y/2)
            w = int(obj.bbox.size.x)
            h = int(obj.bbox.size.y)
            self.filter_kernel_size = (5,5)
            self.depth_threshold = 0
            depth_image_roi = cv_image[y:y+h, x:x+w]
            _, depth_thresholded = cv2.threshold(depth_image_roi, self.depth_threshold, 255, cv2.THRESH_BINARY)
            depth_filtered = cv2.GaussianBlur(depth_thresholded, self.filter_kernel_size, 0)
            contours, _ = cv2.findContours(depth_filtered.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            # interested in the largest contour only:
            if len(contours) > 0:
                largest_contour = max(contours, key=cv2.contourArea)
                M = cv2.moments(largest_contour)
                if M["m00"] != 0:  # avoid division by zero
                    cx = int(M["m10"] / M["m00"])  # centroid x
                    cy = int(M["m01"] / M["m00"])  # centroid y
                else:
                    if self.debug_:
                        self.get_logger().warn("[Yolo2PoseNode::depthCallback] Moment computation resulted in division by zero")
                    continue
                depth_at_centroid = depth_image_roi[cy, cx]

                # Use centroid pixel and depth_at_centroid for your further processing:
                pixel = [x + cx, y + cy]
                pose_msg = self.depthToPoseMsg(pixel, depth_at_centroid)
                transformed_pose_msg = self.transformPose(pose_msg, transform)

                if transformed_pose_msg is not None:
                    poses_msg.poses.append(transformed_pose_msg)
        
        center_coordinates = (int(obj.bbox.center.position.x), int(obj.bbox.center.position.y))
        cv2.ellipse(cv_image, center_coordinates, (int(w/2), int(h/2)), 0, 0, 360, ellipse_color, 1)
        cv2.putText(cv_image, "YOLO", (50, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, text_color, 2)
        image_msg = self.cv_bridge_.cv2_to_imgmsg(cv_image, encoding="passthrough")
        self.overlay_ellipses_image_yolo_.publish(image_msg)
        return poses_msg 


    def kf_process_pose(self, msg: Image):
        kf_msg = copy.deepcopy(self.latest_kf_tracks_msg_)
        # depth_roi_ = self.get_parameter('depth_roi').value
        depth_roi_ = self.get_parameter('depth_roi').value
        nearest_depth_value = None
        min_distance = float('inf')
        kf_transformed_pose_msg = Pose()
        nearest_centroid_x = 0.0 
        nearest_centroid_y = 0.0
        try:
            transform = self.tf_buffer_.lookup_transform(
                self.reference_frame_,
                msg.header.frame_id,
                rclpy.time.Time(),
                timeout=rclpy.duration.Duration(seconds=1.0))
        except TransformException as ex:
            self.get_logger().error(
                f'[Yolo2PoseNode::depthCallback] Could not transform {self.reference_frame_} to {msg.header.frame_id}: {ex}')
            return

        poses_msg_kf = PoseArray()
        poses_msg_kf.header = copy.deepcopy(msg.header)
        poses_msg_kf.header.frame_id = self.reference_frame_
        poses_msg_kf.poses.clear()
        # depth_range = []
        depth_image_cv = self.cv_bridge_.imgmsg_to_cv2(msg, desired_encoding='passthrough')    
        image_height, image_width = depth_image_cv.shape[:2]   
        self.latest_pixels_, self.latest_covariances_2d_, self.latest_depth_ranges_ = self.process_and_store_track_data(self.latest_kf_tracks_msg_)
        # Iterate over the detected objects' mean pixels, covariance matrices, and depth ranges.
        for mean_pixel, covariance_matrix, depth_range in zip(self.latest_pixels_, self.latest_covariances_2d_, self.latest_depth_ranges_):
            # Compute the eigenvalues and eigenvectors of the covariance matrix to determine the ellipse parameters.
            eigenvalues, eigenvectors = np.linalg.eig(covariance_matrix[:2, :2])
            # Calculate the angle of rotation for the ellipse based on the eigenvectors.
            rotation_angle = np.degrees(np.arctan2(eigenvectors[1, 0], eigenvectors[0, 0]))
            # Define the axes lengths for the ellipse based on the eigenvalues.
            axes_lengths = (int(depth_roi_ * np.sqrt(eigenvalues[0])), int(depth_roi_ * np.sqrt(eigenvalues[1])))
            # Create a mask image where the pixels within the ellipse are white (255) and others are black (0).
            mask_image = np.zeros(depth_image_cv.shape, dtype=np.uint8)
            x, y = mean_pixel
            if 0 <= x < image_width and 0 <= y < image_height:

                cv2.ellipse(depth_image_cv, tuple(mean_pixel), axes_lengths, rotation_angle, 0, 360, (0, 255, 0), 1)
            else:
                print(f"Mean pixel {mean_pixel} is outside the image size.")
            # Create a mask for depth values within the specified range.
            # Apply the mask to the depth image to isolate the depth values within the range.
            # masked_depth_image = cv2.bitwise_and(depth_image_cv, depth_image_cv, mask=depth_mask)
            # Find contours in the depth mask which indicate the edges of objects.
            # kernel = np.ones((5,5),np.uint8)
            # erosion = cv2.erode(depth_image_cv,kernel,iterations = 1)     
            depth_image_blurred = cv2.GaussianBlur(depth_image_cv, (5, 5), 0)

            depth_mask = cv2.inRange(depth_image_blurred, depth_range[0], depth_range[1])
       
            kfcontours, _ = cv2.findContours(depth_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)


            for kfcontour in kfcontours:
                # Calculate the moments of the contour, which are used to find the centroid.
                contour_moments = cv2.moments(kfcontour)
                if contour_moments["m00"] != 0:
                    # Compute the centroid coordinates from the moments.
                    centroid_x = int(contour_moments["m10"] / contour_moments["m00"])
                    centroid_y = int(contour_moments["m01"] / contour_moments["m00"])
                    # print("Centroid_X " , centroid_x, " Centroid_Y " , centroid_y)
                    # Ensure the centroid coordinates are within the bounds of the image.
                    if 0 <= centroid_x < depth_image_cv.shape[1] and 0 <= centroid_y < depth_image_cv.shape[0]:
                        contour_depth_values = depth_image_cv[kfcontour[:, :, 1], kfcontour[:, :, 0]]
                        valid_depth_indices = np.logical_and(depth_range[0] <= contour_depth_values, contour_depth_values <= depth_range[1])
                        if np.all(valid_depth_indices):
                            # All depth values satisfy the conditions within the specified range
                            valid_contour_depth_values = contour_depth_values
                       

                            # print("valid_contour_depth_values " , valid_contour_depth_values)
                            if len(valid_contour_depth_values) > 0:
                                average_depth = np.mean(valid_contour_depth_values)
                                distance = np.sqrt((mean_pixel[0] - centroid_x) ** 2 + (mean_pixel[1] - centroid_y) ** 2)

                                
                                # print(" Average_depth " , average_depth)
                                # Update nearest centroid and depth value if this is the closest one yet
                                if distance < min_distance:
                                    min_distance = distance
                                    nearest_depth_value = average_depth
                                    nearest_centroid_x = centroid_x
                                    nearest_centroid_y = centroid_y

        # print(" nearest_depth_value " , nearest_depth_value)
        # Check if the depth value is within the expected range.
        if nearest_depth_value is not None and depth_range[0] <= nearest_depth_value <= depth_range[1]:
            # If yes, convert the pixel coordinates and depth value to a 3D pose.
            pixel_pose = [nearest_centroid_x, nearest_centroid_y]
            kf_pose_msg = self.depthToPoseMsg(pixel_pose, nearest_depth_value)
            # Transform the pose to a different coordinate frame if necessary.
            kf_transformed_pose_msg = self.transformPose(kf_pose_msg, transform)

            if(kf_transformed_pose_msg is not None):
                poses_msg_kf.poses.append(kf_transformed_pose_msg)
        else:
            # Log a warning if the depth value is out of the expected range.
            self.get_logger().warn(
                f"[Yolo2PoseNode::kf_process_pose] Depth value {nearest_depth_value} Small {depth_range[0]} Big {depth_range[1]} at centroid ({nearest_centroid_x}, {nearest_centroid_y}) is out of range."
            )
        # cv2.ellipse(depth_mask, tuple(mean_pixel), axes_lengths, rotation_angle, 0, 360, (0, 255, 0), 1)
        cv2.putText(depth_image_cv, "KF", (50, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

        # Convert the modified depth image with ellipses to a ROS Image message
        ellipses_image_msg = self.cv_bridge_.cv2_to_imgmsg(depth_image_cv, encoding="passthrough")

        # Publish the modified depth image with ellipses on a ROS2 topic
        self.overlay_ellipses_image_yolo_.publish(ellipses_image_msg)

        return poses_msg_kf  
        # self.latest_pixels_ = []
        # self.latest_covariances_2d_ = []
        # self.latest_depth_ranges_   = []  
        
    def publish_transformed_pose(self):
        try:
            pose_values = self.actual_pose()

            if pose_values is not None:  # Check if pose_values is not None
                pose_x, pose_y, pose_z = pose_values

                # Create a Pose message with the obtained transform
                pose_msg = Pose()
                pose_msg.position.x = float(pose_x)
                pose_msg.position.y = float(pose_y)
                pose_msg.position.z = float(pose_z)
                # pose_msg.orientation = pose_orientation
                # actual_pose_values = self.actual_pose()
                self.collect_data(self.latest_kf_tracks_msg_, pose_values) 
                # Publish the transformed pose
                self.pose_publisher.publish(pose_msg)
            else:
                self.get_logger().warn("Pose values are None.")

        except (tf2_ros.LookupException, tf2_ros.ConnectivityException, tf2_ros.ExtrapolationException) as e:
            self.get_logger().warn(f"Exception occurred: {e}")


    def actual_pose(self):
        try:
            transform = self.tf_buffer_.lookup_transform('interceptor/odom', 'target/base_link', rclpy.time.Time())
            translation = transform.transform.translation
            rotation = transform.transform.rotation

            # Extract pose values
            pose_x = translation.x
            pose_y = translation.y
            pose_z = translation.z
            # pose_orientation = rotation

            return pose_x, pose_y, pose_z 
            
        except (tf2_ros.LookupException, tf2_ros.ConnectivityException, tf2_ros.ExtrapolationException) as e:
            self.get_logger().warn(f"Exception occurred: {e}")
            return 0,0,0  # Return a default value in case of exception




    def caminfoCallback(self,msg: CameraInfo):
        # TODO : fill self.camera_info_ field
        P = np.array(msg.p)
        K = np.array(msg.k)
        # if len(P) == 12: # Sanity check
        #     P = P.reshape((3,4))
        #     self.camera_info_ = {'fx': P[0][0], 'fy': P[1][1], 'cx': P[0][2], 'cy': P[1][2]}

        if len(K) == 9: # Sanity check
            K = K.reshape((3,3))
            self.camera_info_ = {'fx': K[0][0], 'fy': K[1][1], 'cx': K[0][2], 'cy': K[1][2]}

    def detectionsCallback(self, msg: DetectionArray):
        self.yolo_detections_msg_ = msg


    def handle_KF_tracker_data(self, msg: KFTracks):
        self.latest_kf_tracks_msg_ = msg

    def process_and_store_track_data(self, msg: KFTracks):

        self.latest_pixels_.clear()
        self.latest_covariances_2d_.clear()
        self.latest_depth_ranges_.clear()

        try:
            
            transform = self.tf_buffer_.lookup_transform(
                # change hardcoded camera msg
                self.camera_frame_,
                msg.header.frame_id,
                rclpy.time.Time(),
                timeout=rclpy.duration.Duration(seconds=1.0))
        except TransformException as ex:
            self.get_logger().error(
                f'[process_and_store_track_data] Could not transform {msg.header.frame_id} to {self.camera_frame_}: {ex}')
            return
        # std_range_ = self.get_parameter('std_range').value
        std_range_ = self.get_parameter('std_range').value


        for track in msg.tracks:
            x = track.pose.pose.position.x
            y = track.pose.pose.position.y
            z = track.pose.pose.position.z

            covariance = track.pose.covariance
            cov_x = covariance[0]
            cov_y = covariance[7]
            cov_z = covariance[14]

            tf2_cam_msg = PoseWithCovarianceStamped()
            tf2_cam_msg.pose.pose.position.x = x
            tf2_cam_msg.pose.pose.position.y = y
            tf2_cam_msg.pose.pose.position.z = z
            tf2_cam_msg.pose.pose.orientation.w = 1.0
            tf2_cam_msg.pose.covariance = [0.0] * 36
            tf2_cam_msg.pose.covariance[0] = cov_x
            tf2_cam_msg.pose.covariance[7] = cov_y
            tf2_cam_msg.pose.covariance[14] = cov_z

            transformed_pose_msg = self.transformPoseWithCovariance(tf2_cam_msg, transform)

            if transformed_pose_msg:
                x_transformed = transformed_pose_msg.pose.pose.position.x
                y_transformed = transformed_pose_msg.pose.pose.position.y
                z_transformed = transformed_pose_msg.pose.pose.position.z
                pixel = self.project_3d_to_2d(x_transformed, y_transformed, z_transformed)

                cov_transformed = transformed_pose_msg.pose.covariance
                cov_x_transformed = cov_transformed[0]
                cov_y_transformed = cov_transformed[7]
                cov_z_transformed = cov_transformed[14]
                covariance_2d = self.project_3d_cov_to_2d(
                    x_transformed, y_transformed, z_transformed, 
                    cov_x_transformed, cov_y_transformed, cov_z_transformed
                )

                depth_range = (
		            max(0, z_transformed -  std_range_ * np.sqrt(cov_z_transformed)),
                    z_transformed +  std_range_ * np.sqrt(cov_z_transformed))
                        
                self.latest_depth_ranges_.append(depth_range)
                self.latest_pixels_.append(pixel)
                self.latest_covariances_2d_.append(covariance_2d)

        
        return self.latest_pixels_, self.latest_covariances_2d_, self.latest_depth_ranges_ 
    
    def project_3d_to_2d(self, x_cam, y_cam, z_cam):
        pixel = [0, 0]
        fx = self.camera_info_['fx']
        fy = self.camera_info_['fy']
        cx = self.camera_info_['cx']
        cy = self.camera_info_['cy']

        # Calculate 2D pixel coordinates from 3D positions (XYZ)
        if z_cam != 0:
            u = int(fx * x_cam / z_cam + cx)
            v = int(fy * y_cam / z_cam + cy)
            pixel = [u, v]
        return pixel
    def project_3d_cov_to_2d(self, x_cam, y_cam, z_cam, cov_x, cov_y, cov_z):
        fx = self.camera_info_['fx']
        fy = self.camera_info_['fy']

        J = np.array([[fx / z_cam, 0, -fx * x_cam / z_cam**2],
                     [0, fy / z_cam, -fy * y_cam / z_cam**2]])  

        covariance_3d = np.diag([cov_x, cov_y, cov_z])
        covariance_2d = J @ covariance_3d @ J.T
        return covariance_2d

    def depthToPoseMsg(self, pixel, depth):
        """
        @brief Computes 3D projections of detections in the camera frame (+X-right, +y-down, +Z-outward)
        @param pixel : xy coordinates in 2D camera frame
        @param depth : Value of pixel
        @return position : 3D projections in camera frame
        """
        pose_msg = Pose()
        if self.camera_info_ is None:
            print("[Yolo2PoseNode::depthToPoseMsg] Camera intrinsic parameters are not available. Skipping 3D projections.")
            return pose_msg

        fx = self.camera_info_['fx']
        fy = self.camera_info_['fy']
        cx = self.camera_info_['cx']
        cy = self.camera_info_['cy']
        u = pixel[0] # horizontal image coordinate
        v = pixel[1] # vertical image coordinate
        d = depth # depth

        x = d*(u-cx)/fx
        y = d*(v-cy)/fy

        pose_msg.position.x = x
        pose_msg.position.y = y
        pose_msg.position.z = float(d)
        pose_msg.orientation.w = 1.0
        
        return pose_msg
    
    def transformPose(self, pose: Pose, tr: TransformStamped) -> Pose:
        """
        @brief Converts 3D positions in the camera frame to the parent_frame
        @param pose:  3D position in the child frame (sensor e.g. camera)
        @param parent_frame: Frame to transform positions to
        @param child_frame: Current frame of positions
        @param tf_time: Time at which positions were computed
        @param tr: Transform 4x4 matrix theat encodes rotation and translation
        @return transformed_pose: Pose of transformed position
        """     
        tf2_pose_msg = TF2Pose()
        tf2_pose_msg.position.x = pose.position.x
        tf2_pose_msg.position.y = pose.position.y
        tf2_pose_msg.position.z = pose.position.z
        tf2_pose_msg.orientation.w = 1.0 
        tr._child_frame_id
   
    
        try:
            transformed_pose = do_transform_pose(tf2_pose_msg, tr)
        except Exception as e:
            self.get_logger().error("[transformPose] Error in transforming point {}".format(e))
            return None

        return transformed_pose
    
    def transformPoseWithCovariance(self, pose: PoseWithCovarianceStamped, tr: TransformStamped) -> PoseWithCovarianceStamped:
        """
        @brief Converts 3D pose with covariance from the frame in pose to the frame in tr
        @param pose:  PoseWithCovarianceStamped
        @param tr: Transform 4x4 matrix theat encodes rotation and translation
        @return pose_with_cov_stamped: PoseWithCovarianceStamped
        """
        
        try:
            pose_with_cov_stamped_msg = do_transform_pose_with_covariance_stamped(pose, tr)
        except Exception as e:
            self.get_logger().error("[transformPoseWithCovariance] Error in transforming pose with covariance {}".format(e))
            return None

        return pose_with_cov_stamped_msg
    


def main(args=None):
    rclpy.init(args=args)
    yolo2pose_node = Yolo2PoseNode()
    yolo2pose_node.get_logger().info("Yolo to Pose conversion node has started")
    rclpy.spin(yolo2pose_node)
    yolo2pose_node.destroy_node()
    rclpy.shutdown()

if __name__ == "__main__":
    main()        


