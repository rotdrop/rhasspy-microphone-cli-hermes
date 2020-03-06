"""Hermes MQTT server for Rhasspy TTS using external program"""
import audioop
import io
import json
import logging
import re
import shlex
import socket
import subprocess
import threading
import time
import typing
import wave
from queue import Queue

import attr
from rhasspyhermes.asr import AsrStartListening, AsrStopListening
from rhasspyhermes.audioserver import (
    AudioDevice,
    AudioDeviceMode,
    AudioDevices,
    AudioFrame,
    AudioGetDevices,
)
from rhasspyhermes.base import Message

_LOGGER = logging.getLogger(__name__)


class MicrophoneHermesMqtt:
    """Hermes MQTT server for Rhasspy microphone input using external program."""

    def __init__(
        self,
        client,
        record_command: typing.List[str],
        sample_rate: int,
        sample_width: int,
        channels: int,
        chunk_size: int = 2048,
        list_command: typing.Optional[typing.List[str]] = None,
        test_command: typing.Optional[str] = None,
        siteId: str = "default",
        output_siteId: typing.Optional[str] = None,
        udp_audio_port: typing.Optional[int] = None,
    ):
        self.client = client
        self.record_command = record_command
        self.sample_rate = sample_rate
        self.sample_width = sample_width
        self.channels = channels
        self.chunk_size = chunk_size
        self.list_command = list_command
        self.test_command = test_command
        self.siteId = siteId

        self.output_siteId = output_siteId or siteId

        self.udp_audio_port = udp_audio_port
        self.udp_output = False
        self.udp_socket: typing.Optional[socket.socket] = None

        self.chunk_queue: Queue = Queue()

        self.audioframe_topic: str = AudioFrame.topic(siteId=self.output_siteId)
        self.test_audio_buffer: typing.Optional[bytes] = None

    # -------------------------------------------------------------------------

    def record(self):
        """Record audio from external program's stdout."""
        try:
            _LOGGER.debug(self.record_command)

            record_proc = subprocess.Popen(self.record_command, stdout=subprocess.PIPE)
            _LOGGER.debug("Recording audio")

            while True:
                chunk = record_proc.stdout.read(self.chunk_size)
                if chunk:
                    self.chunk_queue.put(chunk)
                else:
                    # Avoid 100% CPU usage
                    time.sleep(0.01)
        except Exception:
            _LOGGER.exception("record")

    def publish_chunks(self):
        """Publish audio chunks to MQTT or UDP."""
        try:
            udp_dest = ("127.0.0.1", self.udp_audio_port)

            while True:
                chunk = self.chunk_queue.get()
                if chunk:
                    if self.test_audio_buffer:
                        # Add to buffer for microphone test
                        self.test_audio_buffer += chunk

                    # MQTT output
                    with io.BytesIO() as wav_buffer:
                        wav_file: wave.Wave_write = wave.open(wav_buffer, "wb")
                        with wav_file:
                            wav_file.setframerate(self.sample_rate)
                            wav_file.setsampwidth(self.sample_width)
                            wav_file.setnchannels(self.channels)
                            wav_file.writeframes(chunk)

                        if self.udp_output:
                            # UDP output
                            wav_bytes = wav_buffer.getvalue()
                            self.udp_socket.sendto(wav_bytes, udp_dest)
                        else:
                            # Publish to audioFrame topic
                            self.client.publish(
                                self.audioframe_topic, wav_buffer.getvalue()
                            )
        except Exception:
            _LOGGER.exception("publish_chunks")

    # -------------------------------------------------------------------------

    def handle_get_devices(
        self, get_devices: AudioGetDevices
    ) -> typing.Optional[AudioDevices]:
        """Get available microphones and optionally test them."""

        if get_devices.modes and (AudioDeviceMode.INPUT not in get_devices.modes):
            return None

        devices: typing.List[AudioDevice] = []

        if self.list_command:
            try:
                # Run list command
                _LOGGER.debug(self.list_command)

                output = subprocess.check_output(
                    self.list_command, universal_newlines=True
                ).splitlines()

                # Parse output (assume like arecord -L)
                name, description = None, ""
                first_mic = True
                for line in output:
                    line = line.rstrip()
                    if re.match(r"^\s", line):
                        description = line.strip()
                        if first_mic:
                            description += "*"
                            first_mic = False
                    else:
                        if name is not None:
                            working = None
                            if get_devices.test:
                                working = self.get_microphone_working(name)

                            devices.append(
                                AudioDevice(
                                    mode=AudioDeviceMode.INPUT,
                                    id=name,
                                    name=name,
                                    description=description,
                                    working=working,
                                )
                            )

                        name = line.strip()
            except Exception:
                _LOGGER.exception("handle_get_devices")
        else:
            _LOGGER.warning("No device list command. Cannot list microphones.")

        return AudioDevices(
            devices=devices, id=get_devices.id, siteId=get_devices.siteId
        )

    def get_microphone_working(self, device_name: str, chunk_size: int = 1024) -> bool:
        """Record some audio from a microphone and check its energy."""
        try:
            # read audio
            assert self.test_command, "Test command is required"
            test_cmd = shlex.split(self.test_command.format(device_name))
            _LOGGER.debug(test_cmd)

            proc = subprocess.Popen(test_cmd, stdout=subprocess.PIPE)
            buffer = proc.stdout.read(chunk_size * 2)
            proc.terminate()

            # compute RMS of debiased audio
            # Thanks to the speech_recognition library!
            # https://github.com/Uberi/speech_recognition/blob/master/speech_recognition/__init__.py
            energy = -audioop.rms(buffer, 2)
            energy_bytes = bytes([energy & 0xFF, (energy >> 8) & 0xFF])
            debiased_energy = audioop.rms(
                audioop.add(buffer, energy_bytes * (len(buffer) // 2), 2), 2
            )

            # probably actually audio
            return debiased_energy > 30
        except Exception:
            _LOGGER.exception("get_microphone_working ({device_name})")
            pass

        return False

    # -------------------------------------------------------------------------

    def on_connect(self, client, userdata, flags, rc):
        """Connected to MQTT broker."""
        try:
            topics = [AudioGetDevices.topic()]

            if self.udp_audio_port is not None:
                self.udp_output = True
                self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                _LOGGER.debug(
                    "Audio will also be sent to UDP port %s", self.udp_audio_port
                )
                topics.extend([AsrStartListening.topic(), AsrStopListening.topic()])

            for topic in topics:
                self.client.subscribe(topic)
                _LOGGER.debug("Subscribed to %s", topic)

            threading.Thread(target=self.publish_chunks, daemon=True).start()
            threading.Thread(target=self.record, daemon=True).start()
        except Exception:
            _LOGGER.exception("on_connect")

    def on_message(self, client, userdata, msg):
        """Received message from MQTT broker."""
        try:
            _LOGGER.debug("Received %s byte(s) on %s", len(msg.payload), msg.topic)

            if msg.topic == AudioGetDevices.topic():
                json_payload = json.loads(msg.payload)
                if self._check_siteId(json_payload):
                    result = self.handle_get_devices(
                        AudioGetDevices.from_dict(json_payload)
                    )
                    if result:
                        self.publish(result)
            elif msg.topic == AsrStartListening.topic():
                json_payload = json.loads(msg.payload)
                if (self.udp_audio_port is not None) and self._check_siteId(
                    json_payload
                ):
                    self.udp_output = False
                    _LOGGER.debug("Enable UDP output")
            elif msg.topic == AsrStopListening.topic():
                json_payload = json.loads(msg.payload)
                if (self.udp_audio_port is not None) and self._check_siteId(
                    json_payload
                ):
                    self.udp_output = True
                    _LOGGER.debug("Disable UDP output")
        except Exception:
            _LOGGER.exception("on_message")

    def publish(self, message: Message, **topic_args):
        """Publish a Hermes message to MQTT."""
        try:
            assert self.client
            topic = message.topic(**topic_args)

            _LOGGER.debug("-> %s", message)
            payload: typing.Union[str, bytes] = json.dumps(attr.asdict(message))

            _LOGGER.debug("Publishing %s char(s) to %s", len(payload), topic)
            self.client.publish(topic, payload)
        except Exception:
            _LOGGER.exception("on_message")

    def _check_siteId(self, json_payload: typing.Dict[str, typing.Any]) -> bool:
        return json_payload.get("siteId", "default") == self.siteId
