import contextlib
import http.client
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import agy_client_profile as profile

class Reply:
    def __init__(self,status=200,chunks=None):
        self.status=status;self.chunks=list(chunks or [b'{"modelVersion":"future-exp-c"}'])
    def getheader(self,name):return 'text/event-stream' if name=='Content-Type' else None
    def read1(self,_):return self.chunks.pop(0) if self.chunks else b''

class Upstream:
    calls=[];reply=Reply;fail=False
    def __init__(self,host,timeout):self.host=host;self.timeout=timeout;self.sock=None;self.closed=False
    def request(self,method,path,body,headers):
        self.calls.append({'host':self.host,'timeout':self.timeout,'method':method,'path':path,'body':body,'headers':headers})
        if self.fail:raise OSError('synthetic upstream failure')
    def getresponse(self):return type(self).reply()
    def close(self):self.closed=True

class ProfileTests(unittest.TestCase):
    def setUp(self):
        Upstream.calls=[];Upstream.reply=Reply;Upstream.fail=False
        self.mock=patch.object(profile.http.client,'HTTPSConnection',Upstream);self.mock.start()
        self.proxy=profile.DesktopProfileProxy(timeout=5)
        self.client=http.client.HTTPConnection('127.0.0.1',self.proxy.server.server_port,timeout=5)
    def tearDown(self):self.client.close();self.proxy.close();self.mock.stop()
    def send(self,body=b'{"model":"gemini-3.8-flash-medium"}',chunked=False,path=None):
        self.client.request('POST',path or self.proxy.prefix+'/v1internal:streamGenerateContent?alt=sse',body,
            {'Authorization':'Bearer synthetic-not-a-secret','Content-Type':'application/json',
             'User-Agent':'antigravity/cli/1.3.3 (aidev_client; os_type=linux; arch=amd64; cl=123; auth_method=consumer)'},encode_chunked=chunked)
        response=self.client.getresponse();return response.status,response.read()
    def test_bodies_responses_and_account_authorization_are_unchanged(self):
        body=b'{"model":"gemini-3.8-flash-medium","request":{"thinkingConfig":{"thinkingBudget":4000}}}'
        status,returned=self.send(body)
        self.assertEqual(status,200);self.assertEqual(returned,b'{"modelVersion":"future-exp-c"}')
        call=Upstream.calls[0];self.assertEqual(call['body'],body)
        self.assertEqual(call['headers']['Authorization'],'Bearer synthetic-not-a-secret')
        self.assertEqual(call['host'],'daily-cloudcode-pa.googleapis.com')
        self.assertEqual(call['path'],'/v1internal:streamGenerateContent?alt=sse')
        self.assertEqual(call['headers']['User-Agent'],'antigravity/2.22.0 (aidev_client; os_type=linux; arch=amd64; auth_method=consumer)')
    def test_chunked_native_requests_are_forwarded_intact(self):
        self.assertEqual(self.send(iter([b'{"model":',b'"medium"}']),True)[0],200)
        self.assertEqual(Upstream.calls[0]['body'],b'{"model":"medium"}')
        self.assertNotIn('Transfer-Encoding',Upstream.calls[0]['headers'])
    def test_web_zero_timeout_has_no_network_deadline_and_normal_limits_remain(self):
        self.send();self.assertEqual(Upstream.calls[-1]['timeout'],5)
        self.client.close();self.proxy.close()
        self.proxy=profile.DesktopProfileProxy(timeout=0)
        self.client=http.client.HTTPConnection('127.0.0.1',self.proxy.server.server_port,timeout=5)
        self.send();self.assertIsNone(Upstream.calls[-1]['timeout'])
    def test_private_loopback_prefix_is_required(self):
        self.assertEqual(self.send(path='/v1internal:streamGenerateContent')[0],404)
        self.assertEqual(Upstream.calls,[])
    def test_arbitrary_paths_are_not_forwarded(self):
        self.assertEqual(self.send(path=self.proxy.prefix+'/redirect/https://example.org')[0],404)
        self.assertEqual(Upstream.calls,[])
    def test_failed_upstream_returns_error_and_never_success(self):
        Upstream.fail=True
        with contextlib.redirect_stderr(io.StringIO()) as output:
            status,_=self.send()
        self.assertEqual(status,502);self.assertNotIn('Bearer',output.getvalue())
    def test_upstream_error_status_and_body_are_preserved(self):
        Upstream.reply=lambda:Reply(503,[b'{"error":"No capacity"}'])
        self.assertEqual(self.send(),(503,b'{"error":"No capacity"}'))
    def test_stream_chunks_and_original_route_names_are_preserved(self):
        Upstream.reply=lambda:Reply(chunks=[b'data: {"modelVersion":"exp-a"}\n\n',b'data: {"modelVersion":"exp-b"}\n\n'])
        self.assertEqual(self.send()[1],b'data: {"modelVersion":"exp-a"}\n\ndata: {"modelVersion":"exp-b"}\n\n')
    def test_shutdown_closes_listener_and_worker(self):
        self.proxy.close();self.assertFalse(self.proxy.thread.is_alive())
        self.assertEqual(self.proxy.server.socket.fileno(),-1)
    def test_rejects_untrusted_endpoints_and_credentials_in_urls(self):
        for endpoint in ('https://example.org','http://daily-cloudcode-pa.googleapis.com','https://user:pass@daily-cloudcode-pa.googleapis.com','https://daily-cloudcode-pa.googleapis.com/path','https://daily-cloudcode-pa.googleapis.com?token=x','https://daily-cloudcode-pa.googleapis.com:8443'):
            with self.subTest(endpoint=endpoint),self.assertRaises(ValueError):profile.DesktopProfileProxy(endpoint)
    def test_rejects_invalid_version(self):
        with self.assertRaises(ValueError):profile.DesktopProfileProxy(version='2.22.0\r\nX-Foo: injected')

if __name__=='__main__':unittest.main()
