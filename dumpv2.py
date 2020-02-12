from enum import Enum

import os
import websocket
import gzip
import time
import datetime
import logging

class Writer():
    def __init__(self, directory: str, prefix: str, url: str):
        self.directory = directory
        self.prefix = prefix
        self.url = url
        
        self.stream = None
        self.time_opened = None
        self.last_time = 0

        self.logger = logging.getLogger('writer')
        self.closed = False

    def no_time_backwards(self, time: int):
        # prevent time from going backwards
        if time < self.last_time:
            self.logger.warn('time is going backwards??!!!')
            return self.last_time
            # no last_time update
        else:
            # update last_time
            self.last_time = time
            return time

    def open(self, time: int):
        if self.closed:
            self.logger.error('already closed')
            return

        # convert from nanosec to sec
        time_sec = time // 1_000_000_000
        # calculate hour in day
        time_hour = (time_sec // 3600) % 24

        if (self.stream == None) or (self.time_opened.hour != time_hour):
            # if this is the first time
            # or
            # make new file each hour (to cut seeking time on read) 

            # this shows if this is the first time
            is_first_time = self.stream == None

            # close previous file
            if not is_first_time:
                self.stream.flush()
                self.stream.close()

            self.logger.info('making new file')

            # this is the first time, open new file
            time_datetime = datetime.datetime.utcfromtimestamp(time_sec)
            time_str = time_datetime.strftime('%Y_%m_%d_%H_%M_%S')
            file_path = os.path.join(self.directory, '%s_%s.gz' % (self.prefix, time_str))
            
            # make directories if not exist
            if not os.path.exists(self.directory):
                os.makedirs(self.directory)

            # open gzip stream
            self.stream = gzip.open(file_path, 'at')

            # record the time opened
            self.time_opened = time_datetime

            if is_first_time:
                # write start line
                self.stream.write('start\t%d\t%s\n' % (time, self.url))

    """write message"""
    def msg(self, msg: str, time: int):
        time = self.no_time_backwards(time)
        self.open(time)
        # write a line
        self.stream.writelines(['msg\t%d\t' % time, msg, '\n'])

    def send(self, msg: str, time: int):
        time = self.no_time_backwards(time)
        self.open(time)
        # write a line
        self.stream.writelines(['send\t%d\t' % time, msg, '\n'])

    def end(self, time: int):
        time = self.no_time_backwards(time)
        self.open(time)
        # write a line
        self.stream.write('end\t%s\n' % time)
        
        self.closed = True
        self.stream.flush()
        self.stream.close()

"""dump WebSocket stream"""
class WebSocketDumper:
    def __init__(self, dir_dump: str, exchange: str, url: str, subscribe):
        self.url = url
        # called when connected
        self.subscribe = subscribe
        
        # create new writer for this dumper
        self.writer = Writer(os.path.join(dir_dump, exchange), exchange, url)
        # WebSocketApp for serving WebSocket stream
        self.ws_app = None
        
        self.logger = logging.getLogger('websocket')

    def send(self, message: str):
        self.ws_app.send(message)
        self.writer.send(message, time.time_ns())

    def do(self):
        self.logger.info('Connecting to [%s]...' % self.url)

        # listeners
        def on_close(ws):
            self.logger.warn('WebSocket closed for [%s]' % self.url)
            self.writer.end(time.time_ns())

        def on_message(ws, message):
            self.writer.msg(message, time.time_ns())

        def on_error(ws, error):
            self.logger.error('Got WebSocket error [%s]:' % self.url)
            self.logger.error(error)

            try:
                ws.close()
            except Exception:
                self.logger.exception('ws.close() failed')

        def on_open(ws):
            self.logger.info('WebSocket opened for [%s]' % self.url)

            if self.subscribe != None:
                try:
                    # Do subscribing process
                    self.subscribe(self)
                except Exception:
                    self.logger.exception('Encountered an error on subscribe')
                    ws.close()

        # Open connection to target WebSocket server
        self.ws_app = websocket.WebSocketApp(self.url,
                                                on_open=on_open,
                                                on_message=on_message,
                                                on_error=on_error,
                                                on_close=on_close)

        try:
            self.ws_app.run_forever()
        except KeyboardInterrupt as e:
            self.logger.warn('Got kill command, ending stream')
            self.writer.end(time.time_ns())
            raise e

class Reconnecter:
    DEFAULT_RECONNECTION_TIME = 1  # default wait time is 1 second
    MAX_RECONNECTION_TIME = 60  # reconnection time will not be more than this value

    def __init__(self, gen_dump):
        self.gen_dump = gen_dump

        self.logger = logging.getLogger('reconnector')

    def do(self):
        # seconds to wait
        time_wait = self.DEFAULT_RECONNECTION_TIME
        # last connection time
        time_connect = None

        while True:
            time_connect = datetime.datetime.utcnow()

            try:
                self.gen_dump().do()
            except KeyboardInterrupt as e:
                raise e
            except Exception:
                self.logger.exception('uncatched error in dumper')

            # wait seconds not to dead loop
            # wait if disconnected in less than 5 minites from connection
            if ((time_connect - datetime.datetime.utcnow()) / datetime.timedelta(minutes=1) <= 5):
                # wait
                self.logger.warn('Waiting %d seconds...' % time_wait)
                time.sleep(time_wait)

                # set wait time as twice as the time before
                time_wait = min(time_wait*2, self.MAX_RECONNECTION_TIME)
            else:
                # reset wait time and do not wait
                time_wait = self.DEFAULT_RECONNECTION_TIME
