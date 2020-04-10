# Copyright 2020, Ben Gruver
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
# list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation and/or
# other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors
# may be used to endorse or promote products derived from this software without
# specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
# ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""
This add-in serves as a bridge between IDEA/PyCharm and Fusion 360.

It provides an http server that the IDE connects to in order to launch a script and connect back to the IDE's debugger.

Since multiple copies of Fusion 360 may be running at the same time, the http server starts listening on a random port.
To allow the IDE to discover the correct port for a given pid, this add-in also listens for and responds to SSDP search
requests.

And finally, in order to run a script on Fusion's main thread, this add-in registers a custom event with Fusion 360.
The http server triggers the custom event, and then the event handler gets run on Fusion's main thread and launches the
script, similarly to how Fusion would normally run it.
"""

import adsk.core
import adsk.fusion
import hashlib
import http.client
from http.server import HTTPServer, BaseHTTPRequestHandler
import importlib
import io
import json
import logging
import os
import socket
import socketserver
import struct
import sys
import threading
import logging.handlers
import traceback
from typing import Optional

# asynchronous event that will be used to launch a script inside fusion 360
RUN_SCRIPT_EVENT = "fusion_idea_addin_run_script"
# asynchronous event that will be used to ask user's confirmation before launching a script inside fusion 360
VERIFY_RUN_SCRIPT_EVENT = "fusion_idea_addin_verify_run_script"
# asynchronous event that will be used to show an error dialog to the user
ERROR_DIALOG_EVENT = "fusion_idea_addin_error_dialog"

# If true, the user must confirm the initial connection of a debugger
REQUIRE_CONFIRMATION = True

if REQUIRE_CONFIRMATION:
    try:
        # noinspection PyUnresolvedReferences
        import rsa
    except ModuleNotFoundError:
        sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), "rsa-4.0-py2.py3-none-any.whl"))
        # noinspection PyUnresolvedReferences
        import rsa
        del(sys.path[-1])


def app():
    return adsk.core.Application.get()


def ui():
    return app().userInterface


logger = logging.getLogger("fusion_idea_addin")
logger.propagate = False


class AddIn(object):
    def __init__(self):
        self._run_script_event_handler: Optional[RunScriptEventHandler] = None
        self._run_script_event: Optional[adsk.core.CustomEvent] = None
        self._verify_run_script_event_handler: Optional[VerifyRunScriptEventHandler] = None
        self._verify_run_script_event: Optional[adsk.core.CustomEvent] = None
        self._error_dialog_event_handler: Optional[ErrorDialogEventHandler] = None
        self._error_dialog_event: Optional[adsk.core.CustomEvent] = None
        self._http_server: Optional[HTTPServer] = None
        self._ssdp_server: Optional[SSDPServer] = None
        self._logging_file_handler: Optional[logging.Handler] = None
        self._logging_dialog_handler: Optional[logging.Handler] = None

        self._trusted_keys = {}

    def start(self):
        try:
            self._logging_file_handler = logging.handlers.RotatingFileHandler(
                filename=os.path.join(os.path.dirname(os.path.realpath(__file__)), "fusion_idea_addin_log.txt"),
                maxBytes=2**20,
                backupCount=1)
            self._logging_file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
            logger.addHandler(self._logging_file_handler)
            logger.setLevel(logging.WARNING)

            try:
                app().unregisterCustomEvent(ERROR_DIALOG_EVENT)
            except Exception:
                pass

            self._error_dialog_event = app().registerCustomEvent(ERROR_DIALOG_EVENT)
            self._error_dialog_event_handler = ErrorDialogEventHandler()
            self._error_dialog_event.add(self._error_dialog_event_handler)

            self._logging_dialog_handler = FusionErrorDialogLoggingHandler()
            self._logging_dialog_handler.setFormatter(logging.Formatter("%(message)s"))
            self._logging_dialog_handler.setLevel(logging.FATAL)
            logger.addHandler(self._logging_dialog_handler)
        except Exception:
            # The logging infrastructure may not be set up yet, so we directly show an error dialog instead
            ui().messageBox("Error while starting fusion_idea_addin.\n\n%s" % traceback.format_exc())
            return

        try:
            try:
                app().unregisterCustomEvent(RUN_SCRIPT_EVENT)
            except Exception:
                pass

            self._run_script_event = app().registerCustomEvent(RUN_SCRIPT_EVENT)
            self._run_script_event_handler = RunScriptEventHandler()
            self._run_script_event.add(self._run_script_event_handler)

            try:
                app().unregisterCustomEvent(VERIFY_RUN_SCRIPT_EVENT)
            except Exception:
                pass

            self._verify_run_script_event = app().registerCustomEvent(VERIFY_RUN_SCRIPT_EVENT)
            self._verify_run_script_event_handler = VerifyRunScriptEventHandler()
            self._verify_run_script_event.add(self._verify_run_script_event_handler)

            # Run the http server on a random port, to avoid conflicts when multiple instances of Fusion 360 are
            # running.
            self._http_server = HTTPServer(("localhost", 0), RunScriptHTTPRequestHandler)

            http_server_thread = threading.Thread(target=self.run_http_server, daemon=True)
            http_server_thread.start()

            ssdp_server_thread = threading.Thread(target=self.run_ssdp_server, daemon=True)
            ssdp_server_thread.start()
        except Exception:
            logger.fatal("Error while starting fusion_idea_addin.", exc_info=sys.exc_info())

    def run_http_server(self):
        logger.debug("starting http server: port=%d" % self._http_server.server_port)
        try:
            with self._http_server:
                self._http_server.serve_forever()
        except Exception:
            logger.fatal("Error occurred while starting the http server.", exc_info=sys.exc_info())

    def run_ssdp_server(self):
        logger.debug("starting ssdp server")
        try:
            with SSDPServer(self._http_server.server_port) as server:
                self._ssdp_server = server
                server.serve_forever()
        except Exception:
            logger.fatal("Error occurred while starting the ssdp server.", exc_info=sys.exc_info())

    def get_trusted_key_nonce(self, key) -> Optional[int]:
        return self._trusted_keys.get(key)

    def set_trusted_key_nonce(self, key, nonce: int):
        self._trusted_keys[key] = nonce

    def stop(self):
        if self._http_server:
            try:
                self._http_server.shutdown()
                self._http_server.server_close()
            except Exception:
                logger.error("Error while stopping fusion_idea_addin's HTTP server.", exc_info=sys.exc_info())
        self._http_server = None

        if self._ssdp_server:
            try:
                self._ssdp_server.shutdown()
                self._ssdp_server.server_close()
            except Exception:
                logger.error("Error while stopping fusion_idea_addin's SSDP server.", exc_info=sys.exc_info())
        self._ssdp_server = None

        try:
            if self._run_script_event_handler and self._run_script_event:
                self._run_script_event.remove(self._run_script_event_handler)

            if self._run_script_event:
                app().unregisterCustomEvent(RUN_SCRIPT_EVENT)
        except Exception:
            logger.error("Error while unregistering fusion_idea_addin's run_script event handler.",
                         exc_info=sys.exc_info())
        self._run_script_event_handler = None
        self._run_script_event = None

        try:
            if self._verify_run_script_event_handler and self._verify_run_script_event:
                self._verify_run_script_event.remove(self._verify_run_script_event_handler)

            if self._verify_run_script_event:
                app().unregisterCustomEvent(VERIFY_RUN_SCRIPT_EVENT)
        except Exception:
            logger.error("Error while unregistering fusion_idea_addin's verify_run_script event handler.",
                         exc_info=sys.exc_info())
        self._verify_run_script_event_handler = None
        self._verify_run_script_event = None

        try:
            if self._error_dialog_event_handler and self._error_dialog_event:
                self._error_dialog_event.remove(self._error_dialog_event_handler)

            if self._error_dialog_event:
                app().unregisterCustomEvent(ERROR_DIALOG_EVENT)
        except Exception:
            logger.error("Error while unregistering fusion_idea_addin's error_dialog event handler.",
                         exc_info=sys.exc_info())
        self._error_dialog_event_handler = None
        self._error_dialog_event = None

        try:
            if self._logging_file_handler:
                self._logging_file_handler.close()
                logger.removeHandler(self._logging_file_handler)
        except Exception:
            ui().messageBox("Error while closing fusion_idea_addin's file logger.\n\n%s" % traceback.format_exc())
        self._logging_file_handler = None

        try:
            if self._logging_dialog_handler:
                self._logging_dialog_handler.close()
                logger.removeHandler(self._logging_dialog_handler)
        except Exception:
            ui().messageBox("Error while closing fusion_idea_addin's dialog logger.\n\n%s" % traceback.format_exc())
        self._logging_dialog_handler = None


# noinspection PyUnresolvedReferences
class RunScriptEventHandler(adsk.core.CustomEventHandler):
    """
    An event handler that can run a python script in the main thread of fusion 360, and initiate debugging.
    """

    # noinspection PyMethodMayBeStatic
    def notify(self, args):
        try:
            args = json.loads(args.additionalInfo)
            script_path = args.get("script")
            debug = int(args["debug"])
            pydevd_path = args["pydevd_path"]

            detach = script_path and debug

            if not script_path and not debug:
                logger.warning("No script provided and debugging not requested. There's nothing to do.")
                return

            sys.path.append(pydevd_path)
            try:
                if debug:
                    sys.path.append(os.path.join(pydevd_path, "pydevd_attach_to_process"))
                    try:
                        import attach_script
                        port = int(args["debug_port"])
                        logger.debug("Initiating attach on port %d" % port)
                        attach_script.attach(port, "localhost")
                        logger.debug("After attach")
                    except Exception:
                        logger.fatal("An error occurred while while starting debugger.", exc_info=sys.exc_info())
                    finally:
                        del(sys.path[-1])  # pydevd_attach_to_process dir

                if script_path:
                    script_path = os.path.abspath(script_path)
                    script_name = os.path.splitext(os.path.basename(script_path))[0]
                    script_dir = os.path.dirname(script_path)

                    sys.path.append(script_dir)
                    try:
                        module = importlib.import_module(script_name)
                        importlib.reload(module)
                        logger.debug("Running script")
                        module.run({"isApplicationStartup": False})
                    except Exception:
                        logger.fatal("Unhandled exception while importing and running script.",
                                     exc_info=sys.exc_info())
                    finally:
                        del(sys.path[-1])  # the script dir
            finally:
                if detach:
                    try:
                        import pydevd
                        logger.debug("Detaching")
                        pydevd.stoptrace()
                    except Exception:
                        logger.error("Error while stopping tracing.", exc_info=sys.exc_info())
        except Exception:
            logger.fatal("An error occurred while attempting to start script.", exc_info=sys.exc_info())
        finally:
            del sys.path[-1]  # The pydevd dir



# noinspection PyUnresolvedReferences
class VerifyRunScriptEventHandler(adsk.core.CustomEventHandler):
    """
    An event handler that will verify the debugger connection with the user, and then launch a script.
    """

    # noinspection PyMethodMayBeStatic
    def notify(self, args):
        try:
            request_json = json.loads(args.additionalInfo)

            (return_value, cancelled) = ui().inputBox(
                "New fusion_idea debugger connection detected.\n"
                "\n"
                "Please enter the debugger's public key hash below to proceed.\n"
                "This can be found in IDEA/PyCharm's console.\n"
                "\n"
                "If you did not initiate or expect this connection, you can press\n"
                "cancel to abort the debugging attempt.", "Debugging Verification")

            if cancelled:
                return

            pubkey_string = request_json["pubkey_modulus"] + ":" + request_json["pubkey_exponent"]

            sha1 = hashlib.sha1()
            sha1.update(pubkey_string.encode())

            expected_hash = bytes.hex(sha1.digest())

            if return_value.upper() == expected_hash.upper():
                inner_request = json.loads(request_json["message"])
                addin.set_trusted_key_nonce(pubkey_string, inner_request["nonce"])
                adsk.core.Application.get().fireCustomEvent(RUN_SCRIPT_EVENT, request_json["message"])
            else:
                ui().messageBox("The public key does not match. Aborting.")
        except Exception:
            logger.fatal("An error occurred while attempting to verify the debugging connection.",
                         exc_info=sys.exc_info())


# noinspection PyUnresolvedReferences
class ErrorDialogEventHandler(adsk.core.CustomEventHandler):
    """An event handler that shows an error dialog to the user."""

    # noinspection PyMethodMayBeStatic
    def notify(self, args):
        ui().messageBox(args.additionalInfo, "fusion_idea_addin error")


class FusionErrorDialogLoggingHandler(logging.Handler):
    """A logging handler that shows a error dialog to the user in Fusion 360."""

    def emit(self, record: logging.LogRecord) -> None:
        adsk.core.Application.get().fireCustomEvent(ERROR_DIALOG_EVENT, self.format(record))


class RunScriptHTTPRequestHandler(BaseHTTPRequestHandler):
    """An HTTP request handler that queues an event in the main thread of fusion 360 to run a script."""

    # noinspection PyPep8Naming
    def do_POST(self):
        logger.debug("Got an http request.")
        content_length = int(self.headers["Content-Length"])
        body = self.rfile.read(content_length).decode()

        try:
            request_json = json.loads(body)

            if REQUIRE_CONFIRMATION:
                pubkey = rsa.PublicKey(int(request_json["pubkey_modulus"]), int(request_json["pubkey_exponent"]))
                pubkey_string = request_json["pubkey_modulus"] + ":" + request_json["pubkey_exponent"]
                rsa.verify(request_json["message"].encode(), bytes.fromhex(request_json["signature"]), pubkey)

                nonce = addin.get_trusted_key_nonce(pubkey_string)

                if nonce is None:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"done")
                    self.finish()
                    adsk.core.Application.get().fireCustomEvent(VERIFY_RUN_SCRIPT_EVENT, json.dumps(request_json))
                    return

                inner_request = json.loads(request_json["message"])

                if inner_request["nonce"] <= nonce:
                    raise ValueError("Invalid nonce")

                addin.set_trusted_key_nonce(pubkey_string, inner_request["nonce"])

            adsk.core.Application.get().fireCustomEvent(RUN_SCRIPT_EVENT, request_json["message"])

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"done")
        except Exception:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(traceback.format_exc().encode())
            logger.error("An error occurred while handling http request.", exc_info=sys.exc_info())


class SSDPRequestHandler(socketserver.BaseRequestHandler):

    def handle(self):
        data = self.request[0].strip()
        socket = self.request[1]

        logger.log(logging.DEBUG, "got ssdp request:\n%s" % data)

        try:
            request_line, headers_text = data.split(b"\r\n", 1)
            headers = http.client.parse_headers(io.BytesIO(headers_text))
        except Exception:
            logger.error("An error occurred while parsing ssdp request:\n%s" % data,
                         exc_info=sys.exc_info())
            return

        if (request_line == b"M-SEARCH * HTTP/1.1" and
                headers["MAN"] == '"ssdp:discover"' and
                headers["ST"] == "fusion_idea:debug"):
            response = ("HTTP/1.1 200 OK\r\n"
                        "ST: fusion_idea:debug\r\n"
                        "USN: pid:%(pid)d\r\n"
                        "Location: 127.0.0.1:%(debug_port)d\r\n\r\n") % {
                           "pid": os.getpid(),
                           "debug_port": self.server.debug_port}

            logger.debug("responding to ssdp request")
            socket.sendto(response.encode("utf-8"), self.client_address)
        else:
            logger.warning("Got an unexpected ssdp request:\n%s" % data)


class SSDPServer(socketserver.UDPServer):

    # Random address in the "administrative" block
    MULTICAST_GROUP = "239.172.243.75"

    def __init__(self, debug_port):
        self.debug_port = debug_port
        self.allow_reuse_address = True
        super().__init__(("", 1900), SSDPRequestHandler)

    def server_bind(self):
        super().server_bind()
        req = struct.pack("=4s4s", socket.inet_aton(self.MULTICAST_GROUP), socket.inet_aton("127.0.0.1"))
        self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, req)

    def handle_error(self, request, client_address):
        logger.error("An error occurred while processing ssdp request.", exc_info=sys.exc_info())


addin = AddIn()


def run(_):
    addin.start()


def stop(_):
    logger.debug("stopping")
    addin.stop()
