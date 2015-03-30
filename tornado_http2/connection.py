import collections
import logging
import struct

from tornado.concurrent import Future
from tornado.escape import native_str, utf8
from tornado import gen
from tornado.http1connection import _GzipMessageDelegate
from tornado.httputil import HTTPHeaders, RequestStartLine, ResponseStartLine, responses, HTTPOutputError
from tornado.ioloop import IOLoop
from tornado.iostream import StreamClosedError
from tornado.log import gen_log

from . import constants
from .hpack import HpackDecoder, HpackEncoder


class Params(object):
    def __init__(self, chunk_size=None, max_header_size=None, decompress=False):
        self.chunk_size = chunk_size or 65536
        self.max_header_size = max_header_size or 65536
        self.decompress = decompress


Frame = collections.namedtuple('Frame', ['type', 'flags', 'stream_id', 'data'])


class Connection(object):
    def __init__(self, stream, is_client, params=None, context=None):
        self.stream = stream
        self.is_client = is_client
        if params is None:
            params = Params()
        self.params = params
        self.context = context
        self._initial_settings_written = Future()

        self.streams = {}
        self.next_stream_id = 1 if is_client else 2
        self.hpack_decoder = HpackDecoder(
            constants.Setting.HEADER_TABLE_SIZE.default)
        self.hpack_encoder = HpackEncoder(
            constants.Setting.HEADER_TABLE_SIZE.default)

    def start(self, delegate):
        fut = self._conn_loop(delegate)
        IOLoop.current().add_future(fut, lambda f: f.result())
        return fut

    def create_stream(self, delegate):
        stream = Stream(self, self.next_stream_id, delegate,
                        context=self.context)
        self.next_stream_id += 2
        self.streams[stream.stream_id] = stream
        return stream

    @gen.coroutine
    def _conn_loop(self, delegate):
        try:
            if self.is_client:
                self.stream.write(constants.CLIENT_PREFACE)
            else:
                preface = yield self.stream.read_bytes(
                    len(constants.CLIENT_PREFACE))
                if preface != constants.CLIENT_PREFACE:
                    raise Exception("expected client preface, got %s" %
                                    preface)
            self._write_frame(self._settings_frame())
            self._initial_settings_written.set_result(None)
            while True:
                frame = yield self._read_frame()
                logging.debug('got frame %r', frame)
                if frame.stream_id == 0:
                    self.handle_frame(frame)
                elif (not self.is_client and
                      frame.type == constants.FrameType.HEADERS):
                    if frame.stream_id in self.streams:
                        raise Exception("already have stream %d",
                                        frame.stream_id)
                    stream = Stream(self, frame.stream_id, None,
                                    context=self.context)
                    stream.set_delegate(delegate.start_request(self, stream))
                    self.streams[frame.stream_id] = stream
                    stream.handle_frame(frame)
                else:
                    self.streams[frame.stream_id].handle_frame(frame)
        except StreamClosedError:
            return
        except GeneratorExit:
            # The generator is being garbage collected; don't close the
            # stream because the IOLoop is going away too.
            return
        except:
            self.stream.close()
            raise

    def handle_frame(self, frame):
        if frame.type == constants.FrameType.SETTINGS:
            self._handle_settings_frame(frame)
        elif frame.type == constants.FrameType.WINDOW_UPDATE:
            # TODO: handle WINDOW_UPDATE
            pass
        else:
            raise Exception("invalid frame type %s for stream 0", frame.type)

    def _write_frame(self, frame):
        logging.debug('sending frame %r', frame)
        # The frame header starts with a 24-bit length. Since `struct`
        # doesn't support 24-bit ints, encode as 32 and slice off the first
        # byte.
        header = struct.pack('>iBBi', len(frame.data), frame.type.value,
                             frame.flags, frame.stream_id)
        encoded_frame = header[1:] + frame.data
        return self.stream.write(encoded_frame)

    @gen.coroutine
    def _read_frame(self):
        header_bytes = yield self.stream.read_bytes(9)
        # Re-attach a leading 0 to parse 24-bit length with struct.
        header = struct.unpack('>iBBi', b'\0' + header_bytes)
        data_len, typ, flags, stream_id = header
        typ = constants.FrameType(typ)
        # Strip the reserved bit off of stream_id
        stream_id = stream_id & 0x7fffffff
        data = yield self.stream.read_bytes(data_len)
        raise gen.Return(Frame(typ, flags, stream_id, data))

    def _settings_frame(self):
        # TODO: parameterize?
        if self.is_client:
            payload = struct.pack('>hi', constants.Setting.ENABLE_PUSH.code, 0)
        else:
            payload = b''
        return Frame(constants.FrameType.SETTINGS, 0, 0, payload)

    def _settings_ack_frame(self):
        return Frame(constants.FrameType.SETTINGS, constants.FrameFlag.ACK,
                     0, b'')

    def _handle_settings_frame(self, frame):
        if frame.flags & constants.FrameFlag.ACK:
            return
        else:
            # TODO: respect changed settings.
            self._write_frame(self._settings_ack_frame())


def _reset_on_error(f):
    def wrapper(self, *args, **kw):
        try:
            return f(self, *args, **kw)
        except Exception:
            self.reset()
            raise
    return wrapper


class Stream(object):
    def __init__(self, conn, stream_id, delegate, context=None):
        self.conn = conn
        self.stream_id = stream_id
        self.set_delegate(delegate)
        self.context = context
        self.finish_future = Future()
        from tornado.util import ObjectDict
        # TODO: remove
        self.stream = ObjectDict(io_loop=IOLoop.current(), close=conn.stream.close)
        self._expected_content_remaining = None
        self._need_delegate_close = False

    def set_delegate(self, delegate):
        self.orig_delegate = self.delegate = delegate
        if self.conn.params.decompress:
            self.delegate = _GzipMessageDelegate(delegate, self.conn.params.chunk_size)

    def handle_frame(self, frame):
        if frame.type == constants.FrameType.HEADERS:
            self._handle_headers_frame(frame)
        elif frame.type == constants.FrameType.DATA:
            self._handle_data_frame(frame)
        elif frame.type == constants.FrameType.RST_STREAM:
            self._handle_rst_stream_frame(frame)
        else:
            raise Exception("invalid frame type %s", frame.type)

    def _handle_headers_frame(self, frame):
        if not (frame.flags & constants.FrameFlag.END_HEADERS):
            raise Exception("Continuation frames not yet supported")
        data = frame.data
        if len(data) > self.conn.params.max_header_size:
            # TODO: this matches the h1 behavior but isn't right.
            gen_log.warning("Unsatisfiable read")
            return
        if frame.flags & constants.FrameFlag.PRIORITY:
            # TODO: support PRIORITY and PADDING
            data = data[5:]
        pseudo_headers = {}
        headers = HTTPHeaders()
        for k, v, idx in self.conn.hpack_decoder.decode(bytearray(data)):
            if k.startswith(b':'):
                pseudo_headers[native_str(k)] = native_str(v)
            else:
                headers.add(native_str(k),  native_str(v))
        if self.conn.is_client:
            status = int(pseudo_headers[':status'])
            start_line = ResponseStartLine('HTTP/2.0', status, responses.get(status, ''))
        else:
            start_line = RequestStartLine(pseudo_headers[':method'],
                                          pseudo_headers[':path'], 'HTTP/2.0')
        self._request_start_line = start_line

        self._need_delegate_close = True
        self.delegate.headers_received(start_line, headers)
        if frame.flags & constants.FrameFlag.END_STREAM:
            self.delegate.finish()
            self.finish_future.set_result(None)

    def _handle_data_frame(self, frame):
        self.delegate.data_received(frame.data)
        if frame.flags & constants.FrameFlag.END_STREAM:
            self._need_delegate_close = False
            self.delegate.finish()
            self.finish_future.set_result(None)

    def _handle_rst_stream_frame(self, frame):
        if self._need_delegate_close:
            self.delegate.on_connection_close()

    def set_close_callback(self, callback):
        # TODO: this shouldn't be necessary
        pass

    def reset(self):
        self.conn._write_frame(Frame(constants.FrameType.RST_STREAM,
                                     0, self.stream_id, b'\x00\x00\x00\x00'))

    @_reset_on_error
    def write_headers(self, start_line, headers, chunk=None, callback=None):
        if (not self.conn.is_client and
            (self._request_start_line.method == 'HEAD' or
             start_line.code == 304)):
            self._expected_content_remaining = 0
        elif 'Content-Length' in headers:
            self._expected_content_remaining = int(headers['Content-Length'])
        header_list = []
        if self.conn.is_client:
            header_list.append((b':method', utf8(start_line.method),
                                constants.HeaderIndexMode.YES))
            header_list.append((b':scheme', b'https',
                                constants.HeaderIndexMode.YES))
            header_list.append((b':path', utf8(start_line.path),
                                constants.HeaderIndexMode.NO))
        else:
            header_list.append((b':status', utf8(str(start_line.code)),
                                constants.HeaderIndexMode.YES))
        for k, v in headers.get_all():
            header_list.append((utf8(k.lower()), utf8(v),
                                constants.HeaderIndexMode.YES))
        data = bytes(self.conn.hpack_encoder.encode(header_list))
        frame = Frame(constants.FrameType.HEADERS,
                      constants.FrameFlag.END_HEADERS, self.stream_id,
                      data)
        self.conn._write_frame(frame)

        return self.write(chunk, callback=callback)

    @_reset_on_error
    def write(self, chunk, callback=None):
        if chunk:
            if self._expected_content_remaining is not None:
                self._expected_content_remaining -= len(chunk)
                if self._expected_content_remaining < 0:
                    raise HTTPOutputError(
                        "Tried to write more data than Content-Length")
            self.conn._write_frame(Frame(constants.FrameType.DATA, 0,
                                         self.stream_id, chunk))
        # TODO: flow control
        if callback is not None:
            callback()
        else:
            future = Future()
            future.set_result(None)
            return future

    @_reset_on_error
    def finish(self):
        if (self._expected_content_remaining is not None and
                self._expected_content_remaining != 0):
            raise HTTPOutputError(
                "Tried to write %d bytes less than Content-Length" %
                self._expected_content_remaining)
        self.conn._write_frame(Frame(constants.FrameType.DATA,
                                     constants.FrameFlag.END_STREAM,
                                     self.stream_id, b''))

    def read_response(self, delegate):
        assert delegate is self.orig_delegate, 'cannot change delegate'
        return self.finish_future
