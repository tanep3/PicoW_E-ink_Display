"""PUSH-only Pico receiver and durable host delivery regressions."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import secrets  # Cache the standard library module before adding pico/ to sys.path.
import socket
import threading
from http.client import HTTPConnection
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from PIL import Image

from ai_news import archive, demo, frame, generator, push

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pico"))
import push_receiver as pico
import transfer_push


def art(index=1):
    image = Image.new("L", (250, 122), 255)
    image.putpixel((index, 10), 0)
    out = BytesIO(); image.save(out, "PNG")
    return frame.normalize(out.getvalue())


class PushTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = archive.Archive(self.root)
        self.image = self.store.publish(art(), {"source_urls": ["https://example.com/1"]})
        self.queue = push.PushQueue(self.store)
        self.config = self.root / "config"
        self.config.write_text("[push]\nhost='192.168.0.172'\nport=16151\nretry_count=2\n"
                               "interval_seconds=30\nattempt_timeout_seconds=60\n"
                               "deadline_seconds=0\n")

    def test_config_defaults_validation_and_gallery_demo(self):
        self.assertEqual((2,30,60,0,16151), (push.load_push_config(self.config).retry_count,
            push.load_push_config(self.config).interval_seconds,
            push.load_push_config(self.config).attempt_timeout_seconds,
            push.load_push_config(self.config).deadline_seconds,
            push.load_push_config(self.config).port))
        for bad in ("retry_count=-1", "attempt_timeout_seconds=0", "port=0",
                    "host='192.168.0.99'"):
            self.config.write_text("[push]\n" + bad + "\n")
            with self.assertRaises(ValueError): push.load_push_config(self.config)
        demo.create_demo(self.root, datetime(2026,10,3,tzinfo=timezone.utc))
        demo_id = next((self.root / "demo").iterdir()).name
        before = self.store.latest()
        job = self.queue.enqueue(demo_id)
        self.assertEqual(demo_id, job["source_id"])
        self.assertEqual(before, self.store.latest())
        self.assertEqual(4000, len(self.queue.pending()[3]))

    def test_two_retries_30_seconds_60_second_attempt_and_failure(self):
        job = self.queue.enqueue(self.image["frame_id"])
        calls, waits = [], []
        def cannot_connect(host, port, seq, frame_id, digest, wire, timeout):
            calls.append((seq, timeout, len(wire)))
            raise push.PushDeliveryError("connection", "refused")
        worker = push.PushWorker(self.queue, self.config, cannot_connect)
        self.assertTrue(worker.process_one(sleep=waits.append))
        self.assertEqual([(job["seq"],60,4000)]*3, calls)
        self.assertEqual(60, sum(waits))
        self.assertEqual(3, self.queue.status(job["seq"])["attempts"])
        self.assertEqual("failed", self.queue.status(job["seq"])["state"])
        self.assertEqual("connection", self.queue.status(job["seq"])["error_code"])
        self.assertFalse(worker.process_one())  # reconnect does not automatically resend
        next_job = self.queue.enqueue(self.image["frame_id"])
        self.assertGreater(next_job["seq"], job["seq"])
        self.assertEqual("queued", next_job["state"])

    def test_restart_preserves_retry_interval(self):
        job = self.queue.enqueue(self.image["frame_id"])
        calls = []
        def cannot_connect(*args):
            calls.append(args[2])
            raise push.PushDeliveryError("connection", "refused")
        first = push.PushWorker(self.queue, self.config, cannot_connect)
        self.assertTrue(first.process_one(sleep=lambda _: True))
        self.assertEqual([job["seq"]], calls)
        self.assertEqual("sending", self.queue.status(job["seq"])["state"])
        self.assertGreater(self.queue.pending()[5], time.time() + 28)

        waits = []
        restarted = push.PushWorker(push.PushQueue(archive.Archive(self.root)),
                                    self.config, cannot_connect)
        self.assertTrue(restarted.process_one(sleep=lambda seconds: waits.append(seconds) or True))
        self.assertEqual([job["seq"]], calls)
        self.assertEqual(1, len(waits))
        self.assertGreater(waits[0], 28)

    def test_newer_selection_cancels_old_retry_and_last_display_wins(self):
        old = self.queue.enqueue(self.image["frame_id"])
        calls = []
        def transport(host, port, seq, frame_id, digest, wire, timeout):
            calls.append(seq)
            if seq == old["seq"]:
                raise push.PushDeliveryError("timeout", "no ACK")
        def wait(_):
            if self.queue.status()["seq"] == old["seq"]:
                self.queue.enqueue(self.image["frame_id"])
        worker = push.PushWorker(self.queue, self.config, transport)
        worker.process_one(sleep=wait)
        self.assertEqual([old["seq"]], calls)
        self.assertEqual("superseded", self.queue.status(old["seq"])["state"])
        newer = self.queue.status()["seq"]
        worker.process_one(sleep=lambda _:None)
        self.assertEqual([old["seq"], newer], calls)
        self.assertEqual("displayed", self.queue.status(newer)["state"])

    def test_in_flight_old_ack_finishes_before_new_send(self):
        old = self.queue.enqueue(self.image["frame_id"])
        calls = []
        def transport(host, port, seq, frame_id, digest, wire, timeout):
            calls.append(seq)
            if seq == old["seq"]:
                self.queue.enqueue(self.image["frame_id"])
        worker = push.PushWorker(self.queue, self.config, transport)
        worker.process_one()
        self.assertEqual("superseded", self.queue.status(old["seq"])["state"])
        newer = self.queue.status()["seq"]
        worker.process_one()
        self.assertEqual([old["seq"], newer], calls)
        self.assertEqual("displayed", self.queue.status(newer)["state"])

    def test_real_tcp_post_ack_and_duplicate_without_redraw(self):
        wire = self.queue.enqueue(self.image["frame_id"])
        row = self.queue.pending()
        seq, frame_id, digest, payload, _, _ = row
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0)); listener.listen(2)
        port = listener.getsockname()[1]
        calls = []
        class Panel:
            def init(self, deadline): calls.append("init")
            def display(self, data, deadline): calls.append("display")
            def sleep(self, deadline): calls.append("sleep")
        state = str(self.root / "pico_state.json")
        errors = []
        def receive():
            try:
                for _ in range(2):
                    client, address = listener.accept()
                    with patch.object(pico, "SENDER_IP", "127.0.0.1"):
                        pico.handle_connection(client, address[0], Panel, state)
            except Exception as exc:
                errors.append(exc)
            finally:
                listener.close()
        thread = threading.Thread(target=receive, daemon=True); thread.start()
        push.send_frame("127.0.0.1", port, seq, frame_id, digest, payload, 5)
        push.send_frame("127.0.0.1", port, seq, frame_id, digest, payload, 5)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(["init", "display", "sleep"], calls)

    def test_ack_after_five_seconds_succeeds_within_total_deadline(self):
        self.queue.enqueue(self.image["frame_id"])
        seq, frame_id, digest, wire, _, _ = self.queue.pending()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0)); listener.listen(1)
        port = listener.getsockname()[1]
        errors = []
        def receive():
            try:
                client, _ = listener.accept()
                with client:
                    request = bytearray()
                    while b"\r\n\r\n" not in request:
                        request.extend(client.recv(512))
                    head, body = bytes(request).split(b"\r\n\r\n", 1)
                    while len(body) < 4000:
                        body += client.recv(4000 - len(body))
                    time.sleep(5.2)
                    answer = json.dumps({"seq":seq,"wire_sha256":digest,"state":"displayed"}).encode()
                    client.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " +
                                   str(len(answer)).encode() + b"\r\n\r\n" + answer)
            except Exception as exc:
                errors.append(exc)
            finally:
                listener.close()
        thread = threading.Thread(target=receive, daemon=True); thread.start()
        push.send_frame("127.0.0.1", port, seq, frame_id, digest, wire, 8)
        thread.join(timeout=8)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)

    def test_pico_body_wait_uses_remaining_sixty_second_budget(self):
        self.queue.enqueue(self.image["frame_id"])
        seq, frame_id, digest, wire, _, _ = self.queue.pending()
        request = (("POST /v1/frame HTTP/1.1\r\nContent-Type: application/octet-stream\r\n"
                    "Content-Length: 4000\r\nX-Push-Seq: %d\r\nX-Frame-ID: %s\r\n"
                    "X-Wire-SHA256: %s\r\nX-Format-ID: %s\r\n\r\n") %
                   (seq, frame_id, digest, frame.FORMAT_ID)).encode() + wire
        class Socket:
            def __init__(self): self.data=request; self.timeouts=[]
            def settimeout(self, seconds): self.timeouts.append(seconds)
            def recv(self, count):
                chunk,self.data=self.data[:count],self.data[count:]
                return chunk
        sock=Socket()
        result=pico.read_frame(sock,pico.ticks_add(pico.ticks_ms(),60_000))
        self.assertEqual(seq,result[0])
        self.assertGreater(min(sock.timeouts),50)

    def test_publication_registration_failure_remains_visible(self):
        root = self.root / "registration_failure"
        class Backend:
            def probe(self): return {"schema_version":1,"output_png":True}
            def generate(self,*args,**kwargs): return art(7)
        with patch.object(generator,"public_url",side_effect=lambda url:url), \
             patch.object(push.PushQueue,"enqueue",side_effect=OSError("queue unavailable")):
            result=generator.run_once(root,Backend(),manual=True,
                now=datetime(2026,10,3,8,tzinfo=timezone.utc),
                news_fetcher=lambda *args:{"source_url":"https://example.com/registration",
                                         "summary":"test"})
        self.assertEqual("published",result)
        archive_result=archive.Archive(root)
        self.assertIsNotNone(archive_result.latest())
        self.assertEqual("registration_failed",push.PushQueue(archive_result).status()["error_code"])
        from ai_news.server import FrameServer
        server=FrameServer(("127.0.0.1",0),archive_result,allowed_network="127.0.0.0/8",
                           config_path=self.config)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            conn=HTTPConnection("127.0.0.1",server.server_port,timeout=5)
            conn.request("GET","/v1/push/status")
            reply=conn.getresponse()
            self.assertEqual(200,reply.status)
            self.assertEqual("registration_failed",json.loads(reply.read())["error_code"])
            conn.close()
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)
        push.PushQueue(archive_result).enqueue(archive_result.latest()["frame_id"])
        self.assertEqual("queued",push.PushQueue(archive_result).status()["state"])

    def test_transfer_backup_before_write_rollback_and_secret_isolation(self):
        import re
        from contextlib import ExitStack
        old = {name: ("old " + name).encode() for name in
               ("protocol.py", "panel_v4.py", "config.py", "main.py")}
        secret = b"private credential sentinel"
        new = {name:(transfer_push.SOURCE / name).read_bytes() for name in transfer_push.FILES}
        class Serial:
            def write(self, data): pass
            def reset_input_buffer(self): pass
            def close(self): pass

        def scenario(fail=False, prepush=False):
            fs = {**old, "secrets.py": secret}
            if prepush: fs["main.py.prepush"] = b"prior rollback"
            events = []
            backup = self.root / ("backup_failure" if fail else
                                   "backup_preexisting" if prepush else "backup_success")
            def names(_): return list(fs)
            def read(_, name):
                self.assertNotEqual("secrets.py",name)
                events.append(("read",name))
                return fs[name]
            def write(_, name, data):
                self.assertTrue(all((backup / item).exists() for item in old))
                self.assertNotEqual("secrets.py",name)
                events.append(("write",name))
                fs[name]=data
            def digest(_, name):
                self.assertNotEqual("secrets.py",name)
                if fail and name == "config.py" and fs.get(name) == new[name]:
                    return "0" * 64
                return frame.sha256(fs[name])
            def execute(_, command, seconds=12):
                for source,target in re.findall(r"os.rename\('([^']+)','([^']+)'\)",command):
                    fs[target]=fs.pop(source); events.append(("rename",source,target))
                for name in re.findall(r"os.remove\('([^']+)'\)",command):
                    fs.pop(name); events.append(("remove",name))
                return b""
            # Rollback's remove precedes rename; model that command in order.
            def ordered_execute(_, command, seconds=12):
                for operation,source,target in re.findall(
                    r"os\.(rename|remove)\('([^']+)'(?:,'([^']+)')?\)",command):
                    if operation == "rename": fs[target]=fs.pop(source)
                    else: fs.pop(source)
                    events.append((operation,source,target))
                return b""
            with ExitStack() as stack:
                stack.enter_context(patch.object(transfer_push.list_ports,"comports",
                    return_value=[types.SimpleNamespace(device="/dev/fake",vid=0x2E8A,pid=0x0005)]))
                stack.enter_context(patch.object(transfer_push.serial,"Serial",return_value=Serial()))
                for method,replacement in (("read_until",lambda *args:b">"),
                    ("device_names",names),("read_file",read),("write_file",write),
                    ("remote_hash",digest),("execute",ordered_execute),
                    ("time",types.SimpleNamespace(sleep=lambda _:None))):
                    stack.enter_context(patch.object(transfer_push,method,replacement))
                if fail or prepush:
                    with self.assertRaises(RuntimeError):
                        transfer_push.run("/dev/fake",backup)
                else:
                    transfer_push.run("/dev/fake",backup)
            self.assertEqual(secret,fs["secrets.py"])
            self.assertFalse(any("secrets.py" in str(event) for event in events))
            if prepush:
                self.assertFalse(any(event[0] == "write" for event in events))
                self.assertEqual(old["main.py"],fs["main.py"])
            elif fail:
                for name in old: self.assertEqual(old[name],fs[name])
                self.assertNotIn("push_receiver.py",fs)
                self.assertFalse(any(event == ("write","main.py.new") for event in events))
            else:
                for name in new: self.assertEqual(new[name],fs[name])
                self.assertEqual(("write","main.py.new"),
                                 [event for event in events if event[0] == "write"][-1])
                for name in old:
                    self.assertEqual(old[name],(backup / name).read_bytes())
                    self.assertEqual(old[name],fs[name + ".prepush"])
                self.assertEqual(0o700,backup.stat().st_mode & 0o777)
                self.assertEqual(0o600,(backup / "main.py").stat().st_mode & 0o777)
        scenario()
        scenario(fail=True)
        scenario(prepush=True)

    def test_transfer_parses_micropython_listing_bytes(self):
        with patch.object(transfer_push, "execute",
                          return_value=b"NAMES:['config.py', 'main.py']\r\n"):
            self.assertEqual(['config.py', 'main.py'], transfer_push.device_names(object()))

    def test_web_push_same_origin_and_latest_unchanged(self):
        from ai_news.server import FrameServer
        server = FrameServer(("127.0.0.1",0), self.store,
                             allowed_network="127.0.0.0/8", config_path=self.config)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            host = "127.0.0.1:%d" % server.server_port
            headers = {"Host":host,"Origin":"http://"+host,"Content-Type":"application/json",
                       "X-AI-News-Action":"push","Sec-Fetch-Site":"same-origin"}
            def request(origin):
                conn = HTTPConnection("127.0.0.1",server.server_port,timeout=5)
                conn.request("POST","/v1/push",json.dumps({"source_id":self.image["frame_id"]}),
                             {**headers,"Origin":origin})
                reply = conn.getresponse(); status,body=reply.status,reply.read();conn.close()
                return status,body
            self.assertEqual(403,request("http://evil.test")[0])
            status,body=request("http://"+host)
            self.assertEqual(202,status)
            seq=json.loads(body)["seq"]
            self.assertEqual(self.image,self.store.latest())
            conn=HTTPConnection("127.0.0.1",server.server_port,timeout=5)
            conn.request("GET","/v1/push/status?seq="+str(seq))
            reply=conn.getresponse();self.assertEqual(200,reply.status)
            self.assertEqual("queued",json.loads(reply.read())["state"]);conn.close()
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)

    def test_pico_error_categories_are_safe_and_distinct(self):
        job = self.queue.enqueue(self.image["frame_id"])
        seq, frame_id, digest, wire, _, _ = self.queue.pending()
        class Socket:
            def __init__(self, status):
                self.status=status; self.packet=b""; self.read=False
            def settimeout(self, value): pass
            def send(self, data): self.packet+=data; return len(data)
            def recv(self, count):
                if self.read: return b""
                self.read=True
                return ("HTTP/1.1 %s\r\nContent-Length: 0\r\n\r\n"%self.status).encode()
            def close(self): pass
        for status,expected in (("400 Bad Request","data_invalid"),
                                ("503 Service Unavailable","panel_failed"),
                                ("504 Gateway Timeout","unknown_result")):
            with self.subTest(status=status), patch.object(push.socket,"create_connection",return_value=Socket(status)):
                with self.assertRaises(push.PushDeliveryError) as failure:
                    push.send_frame("192.168.0.172",16151,seq,frame_id,digest,wire,60)
                self.assertEqual(expected,failure.exception.code)

    def test_wifi_reconnect_does_not_fetch_or_change_old_image(self):
        state = str(self.root / "pico_state.json")
        digest = frame.sha256(self.queue.enqueue(self.image["frame_id"])["frame_id"].encode())
        pico.save_state(55, digest, state)
        before = Path(state).read_bytes()
        calls = []
        class StopLoop(BaseException): pass
        class WLAN:
            def isconnected(self): return False
            def disconnect(self): calls.append("disconnect")
            def active(self, value): calls.append(("active",value))
        class Listener:
            def setsockopt(self,*args): pass
            def bind(self,*args): pass
            def listen(self,*args): pass
            def setblocking(self,*args): pass
            def close(self): calls.append("closed")
        class Poller:
            def register(self,*args): pass
            def poll(self,*args): calls.append("polled"); return []
        with patch.object(pico,"connect_wifi",side_effect=[WLAN(),StopLoop()]) as connect, \
             patch.object(pico.socket,"socket",return_value=Listener()), \
             patch.object(pico,"time",types.SimpleNamespace(sleep_ms=lambda _:None)), \
             patch.dict(sys.modules,{"select":types.SimpleNamespace(POLLIN=1,poll=Poller)}):
            with self.assertRaises(StopLoop): pico.serve_forever()
        self.assertEqual(2,connect.call_count)
        self.assertIn("disconnect",calls)
        self.assertIn(("active",False),calls)
        self.assertEqual(before,Path(state).read_bytes())
        self.assertNotIn("polled",calls)

    def test_lost_ack_is_unknown_not_claimed_undisplayed(self):
        job = self.queue.enqueue(self.image["frame_id"])
        wire = self.queue.pending()[3]
        class Socket:
            def __init__(self): self.packet = b""; self.closed=False
            def settimeout(self, value): pass
            def send(self, data): self.packet += data; return len(data)
            def recv(self, count): return b""
            def close(self): self.closed=True
        fake = Socket()
        with patch.object(push.socket, "create_connection", return_value=fake):
            with self.assertRaises(push.PushDeliveryError) as failure:
                push.send_frame("192.168.0.172",16151,job["seq"],self.image["frame_id"],
                                frame.sha256(wire),wire,60)
        self.assertEqual("unknown_result", failure.exception.code)
        self.assertIn(b"POST /v1/frame HTTP/1.1", fake.packet)
        self.assertTrue(fake.packet.endswith(wire))

    def test_manual_publication_auto_queues_push_and_preserves_hourly_slot(self):
        root = self.root / "manual_generation"
        class Backend:
            def probe(self): return {"schema_version":1,"output_png":True}
            def generate(self,*args,**kwargs): return art(3)
        now = datetime(2026,10,3,8,tzinfo=timezone.utc)
        with patch.object(generator,"public_url",side_effect=lambda url:url):
            self.assertEqual("published",generator.run_once(root,Backend(),now=now,manual=True,
                news_fetcher=lambda *args:{"source_url":"https://example.com/manual","summary":"test"}))
        store=archive.Archive(root)
        queue=push.PushQueue(store)
        self.assertEqual("queued",queue.status()["state"])
        self.assertEqual(store.latest()["frame_id"],queue.status()["frame_id"])
        self.assertFalse(store.job_exists(int(now.timestamp())//3600))

    def test_published_image_survives_failed_push_and_next_timer_runs(self):
        root = self.root / "generation"
        backend_calls = []
        class Backend:
            def probe(self): return {"schema_version":1,"output_png":True}
            def generate(self,*args,**kwargs):
                backend_calls.append(1)
                return art(len(backend_calls))
        choices = []
        def fetch(*args):
            choices.append(1)
            return {"source_url":"https://example.com/"+str(len(choices)),"summary":"test"}
        first = datetime(2026,10,3,8,tzinfo=timezone.utc)
        with patch.object(generator,"public_url",side_effect=lambda url:url):
            self.assertEqual("published",generator.run_once(root,Backend(),now=first,news_fetcher=fetch))
            store = archive.Archive(root)
            queue = push.PushQueue(store)
            seq = queue.status()["seq"]
            def fail(*args): raise push.PushDeliveryError("connection","offline")
            push.PushWorker(queue,self.config,fail).process_one(sleep=lambda _:None)
            self.assertEqual("failed",queue.status(seq)["state"])
            self.assertEqual(1,store.latest()["publish_seq"])
            self.assertEqual("published",generator.run_once(root,Backend(),
                            now=first+timedelta(hours=1),news_fetcher=fetch))
            self.assertEqual(2,store.latest()["publish_seq"])
            self.assertEqual("queued",queue.status()["state"])
            self.assertGreater(queue.status()["seq"],seq)
            push.PushWorker(queue,self.config,lambda *args:None).process_one()
            self.assertEqual("displayed",queue.status()["state"])


if __name__ == "__main__": unittest.main()
