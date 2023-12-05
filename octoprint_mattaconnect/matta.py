import time
import json
import threading
import backoff
from octoprint.util.platform import get_os
from octoprint.util.version import get_octoprint_version_string

from .utils import (
    inject_auth_key,
    make_timestamp,
    get_cloud_http_url,
    get_cloud_websocket_url,
    get_current_memory_usage,
    generate_auth_headers,
)
from .printer import MattaPrinter
from .ws import Socket
from .data import DataEngine
import requests


class MattaCore:
    def __init__(self, plugin, csv_capture=True, image_capture=True):
        self._settings = plugin._settings
        self._printer = MattaPrinter(plugin._printer, plugin._logger, plugin._file_manager, settings=plugin._settings)
        self._logger = plugin._logger
        self._file_manager = plugin._file_manager
        self._logger.debug("Starting MattaConnect Plugin...")
        self.nozzle_camera_count = 0
        self.ws = None
        self.ws_loop_time = 5
        self.terminal_cmds = []
        # get OS type (linux, windows, mac)
        self.os = get_os()
        # get OctoPrint version
        self.octoprint_version = get_octoprint_version_string()

        self.user_online = False
        self.start_websocket_thread()

        self.data_engine = DataEngine(self._printer, self._settings, self._logger)

    def start_websocket_thread(self):
        """Starts the main WS thread."""
        self._logger.debug("Setting up main WS thread...")
        ws_data_thread = threading.Thread(target=self.websocket_thread_loop)
        ws_data_thread.daemon = True
        ws_data_thread.start()
        self._logger.debug("Main WS thread running.")

    def update_ws_send_interval(self):
        """
        Updates the WebSocket send interval based on the current print job status.
        """
        if self.user_online and self._printer.has_job():
            # When the user is online and printer is printing
            self.ws_loop_time = 1.25 # 1250ms websocket send interval
        elif self.user_online:
            # When the user is online
            self.ws_loop_time = 1.25  # 1250ms websocket send interval
        else:
            # When the user is offline
            self.ws_loop_time = 30  # 30s websocket send interval

    def ws_connected(self):
        """
        Checks if the WebSocket connection is currently connected.

        Returns:
            bool: True if the WebSocket connection is connected, False otherwise.

        """
        if hasattr(self, "ws") and self.ws is not None:
            if self.ws.connected():
                return True
        return False

    def ws_connect(self, wait=True):
        """
        Connects to the WebSocket server.

        Args:
            wait (bool): Indicates whether to wait for a few seconds after connecting.
        """
        self._logger.debug("Connecting websocket")
        try:
            full_url = get_cloud_websocket_url() + "api/v1/ws/printer"
            if self.ws_connected():
                self.ws.disconnect()
                self.ws = None
                if self.ws_thread:
                    self.ws_thread.join()
                    self.ws_thread = None
            self.ws = Socket(
                on_message=lambda ws, msg: self.ws_on_message(msg),
                url=full_url,
                token=self._settings.get(["auth_token"]),
            )
            self.ws_thread = threading.Thread(target=self.ws.run)
            self.ws_thread.daemon = True
            self.ws_thread.start()
            if wait:
                time.sleep(2) # wait for 2 seconds
        except Exception as e:
            self._logger.debug("ws_on_close: %s", e)
            pass

    def ws_on_message(self, msg):
        """
        Callback function called when a message is received over the WebSocket connection.

        Args:
            ws: The WebSocket instance.
            msg (str): The received message.

        """
        json_msg = json.loads(msg)
        self._logger.debug("ws_on_message: %s", json_msg)
        if (
            json_msg["token"] == self._settings.get(["auth_token"])
            and json_msg["interface"] == "client"
        ):
            if json_msg.get("state", None) == "online":
                self.user_online = True
                msg = self.ws_data()
            elif json_msg.get("state", None) == "offline":
                self.user_online = False
                msg = self.ws_data()
            elif json_msg.get("webrtc", None) == "request":
                # check if auth_key has already been received
                webrtc_auth_key = json_msg.get("auth_key", None)
                last_webrtc_auth_key = self._settings.get(["webrtc_auth_key"])
                if webrtc_auth_key is not None and webrtc_auth_key != last_webrtc_auth_key:
                    # save auth_key
                    self._settings.set(["webrtc_auth_key"], webrtc_auth_key, force=True)
                    self._settings.save()
                    webrtc_data = self.request_webrtc_stream()
                    webrtc_data = inject_auth_key(webrtc_data, json_msg, self._logger)
                    msg = self.ws_data(extra_data=webrtc_data)
            elif json_msg.get("webrtc", None) == "remote_candidate":
                webrtc_data = self.remote_webrtc_stream(candidate=json_msg["data"])
                webrtc_data = inject_auth_key(webrtc_data, json_msg, self._logger)
                msg = self.ws_data(extra_data=webrtc_data)
            elif json_msg.get("webrtc", None) == "offer":
                webrtc_data = self.connect_webrtc_stream(offer=json_msg["data"])
                webrtc_data = inject_auth_key(webrtc_data, json_msg, self._logger)
                msg = self.ws_data(extra_data=webrtc_data)
            else:
                self._printer.handle_cmds(json_msg)
                msg = self.ws_data()
        self.ws_send(msg)
        self.update_ws_send_interval()
    
    def ws_send(self, msg):
        """
        Sends a message over the WebSocket connection.

        Args:
            msg (str): The message to send.

        """
        try:
            if self.ws_connected():
                self.ws.send_msg(msg)
        except Exception as e:
            self._logger.debug("ws_send: %s", e)
        

    def ws_data(self, extra_data=None):
        """
        Generates the data payload to be sent over the WebSocket connection.

        Args:
            extra_data (dict): Additional data to include in the payload.

        Returns:
            dict: The data payload.

        """
        data = {
            "token": self._settings.get(["auth_token"]),
            "timestamp": make_timestamp(),
            "files": self._file_manager.list_files(recursive=True),
            "terminal_cmds": self.terminal_cmds,
            "system": {
                "version": self.octoprint_version,
                "os": self.os,
                "memory": get_current_memory_usage(self.os),
            },
            "nozzle_tip_coords": {
                "nozzle_tip_coords_x": int(self._settings.get(["nozzle_tip_coords_x"])),
                "nozzle_tip_coords_y": int(self._settings.get(["nozzle_tip_coords_y"])),
            },
            "webcam_transforms": {
                "flip_h": self._settings.get(["flip_h"]),
                "flip_v": self._settings.get(["flip_v"]),
                "rotate": self._settings.get(["rotate"]),
            },
        }
        if self._printer.connected():
            printer_data = self._printer.get_data()
            data.update(printer_data)
        if extra_data:
            data.update(extra_data)
        return data

    def test_auth_token(self, token):
        """
        Tests the validity of an authorization token.

        Args:
            token (str): The authorization token to test.

        Returns:
            tuple: A tuple containing a boolean indicating success and a status message.
        """
        full_url = get_cloud_http_url() + "api/v1/printers/ping"
        success = False
        status_text = "Oh no! An unknown error occurred."
        if token == "":
            status_text = "Please enter a token."
            return success, status_text
        self._settings.set(["auth_token"], token, force=True)
        self._settings.save()
        try:
            headers = generate_auth_headers(token)
            resp = requests.get(
                url=full_url,
                headers=headers,
                timeout=5,
            )
            if resp.status_code == 200:
                self._settings.set(["auth_token"], token, force=True)
                self._settings.save()
                if self.ws_connected():
                    self.ws.disconnect()
                status_text = "All is tickety boo! Your token is valid."
                success = True
            elif resp.status_code == 401:
                status_text = "Whoopsie. That token is invalid."
            else:
                status_text = "Oh no! An unknown error occurred."
        except requests.exceptions.RequestException as e:
            self._logger.warning(
                "Testing authorization token: %s, URL: %s, Headers %s",
                e,
                full_url,
                generate_auth_headers(token),
            )
            status_text = "Error. Please check OctoPrint's internet connection"
        return success, status_text
    
    def take_snapshot(self, url):
        """
        Takes a snapshot of the current print job.

        Args:
            url (str): The URL to send the snapshot to.

        Returns:
            Image: The snapshot image.
        """
        success = False
        image = None
        status_text = "Oh no! An unknown error occurred."
        if url == "":
            status_text = "Please enter a URL."
            return success, status_text, image
        self._settings.set(["snapshot_url"], url.strip(), force=True)
        self._settings.save()
        try:
            resp = requests.get(self._settings.get(["snapshot_url"]), stream=True)  # Add a timeout
        except requests.exceptions.RequestException as e:
            self._logger.debug("Error when sending request: %s", e)
            status_text = "Error when sending request: " + str(e)
            return success, status_text, image
        if resp.status_code == 200:
            success = True
            image = resp.content
            status_text = "Image captured successfully."
        else:
            status_text = "Error: received status code " + str(resp.status_code)
        return success, status_text, image

    def websocket_thread_loop(self):
        """
        Sends data over the WebSocket connection.

        This method continuously sends data while the WebSocket connection is active.
        It uses an exponential backoff strategy for reconnection attempts.

        """
        old_time = time.perf_counter()
        time_buffer = 0.0
        while True:
            try:
                self.ws_connect()
                while self.ws_connected():
                    current_time = time.perf_counter()
                    if (current_time - old_time) > self.ws_loop_time - time_buffer:
                        time_buffer = max(
                            0, current_time - old_time - self.ws_loop_time
                        )
                        old_time = current_time
                        msg = self.ws_data()
                        self.ws.send_msg(msg)
                    time.sleep(0.1)  # slow things down to 100ms
                    self.update_ws_send_interval()
            except Exception as e:
                self._logger.debug("ERROR websocket_thread_loop: %s", e)
                if self.ws_connected():
                    self.ws.disconnect()
                    self.ws = None
            finally:
                try:
                    if self.ws_connected():
                        self.ws.disconnect()
                        self.ws = None
                except Exception as e:
                    self._logger.debug("ERROR ws_send_data: %s", e)
            time.sleep(0.1)  # slow things down to 100ms

    def request_webrtc_stream(self):
        """
        Initiates a WebRTC stream by sending a request to the /webcam/webrtc endpoint.

        Note: This method only works with the new camera-streamer stack.

        Returns:
            dict: A dictionary containing WebRTC data if the request is successful, None otherwise.
        """
        self._logger.info("Requesting WebRTC stream")
        ice_servers = [{"urls": ["stun:stun.l.google.com:19302"]}]
        params = {
            "type": "request",
            "res": None,
            "iceServers": ice_servers,
        }
        headers = {"Content-Type": "application/json"}
        try:
            resp = requests.post(
                self._settings.get(["webrtc_url"]), json=params, headers=headers, timeout=5,
            )
            if resp.status_code == 200:
                return {"webrtc_data": resp.json()}
        except requests.exceptions.RequestException as e:
            self._logger.debug("ERROR RequestException: ", e)
        except Exception as e:
            self._logger.debug("ERROR: ", e)
        return None

    def remote_webrtc_stream(self, candidate):
        """
        Sends WebRTC candidate data to the /webcam/webrtc endpoint to establish a remote stream.

        Note: This method only works with the new camera-streamer stack.

        Args:
            candidate (dict): The WebRTC candidate data.

        Returns:
            dict: A dictionary containing WebRTC data if the request is successful, None otherwise.
        """
        params = {
            "type": candidate["type"],
            "id": candidate["id"],
            "candidates": candidate["candidates"],
        }
        headers = {"Content-Type": "application/json"}
        try:
            resp = requests.post(
                self._settings.get(["webrtc_url"]), json=params, headers=headers, timeout=5,
            )
            if resp.status_code == 200:
                return {"webrtc_data": resp.json()}
        except requests.exceptions.RequestException as e:
            self._logger.debug("ERROR RequestException: ", e)
        except Exception as e:
            self._logger.debug("ERROR: ", e)
        return None

    def connect_webrtc_stream(self, offer):
        """
        Sends WebRTC offer data to the /webcam/webrtc endpoint to establish a WebRTC stream.

        Note: This method only works with the new camera-streamer stack.

        Args:
            offer (dict): The WebRTC offer data.

        Returns:
            dict: A dictionary containing WebRTC data if the request is successful, None otherwise.
        """
        params = {
            "type": offer["type"],
            "id": offer["id"],
            "sdp": offer["sdp"],
        }
        headers = {"Content-Type": "application/json"}
        try:
            resp = requests.post(
                self._settings.get(["webrtc_url"]), json=params, headers=headers, timeout=5,
            )
            if resp.status_code == 200:
                return {"webrtc_data": resp.json()}
        except requests.exceptions.RequestException as e:
            self._logger.debug("ERROR RequestException: ", e)
        except Exception as e:
            self._logger.debug("ERROR: ", e)
        return None
