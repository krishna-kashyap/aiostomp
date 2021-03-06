import sys
import asyncio
import argparse
import random
import string
import math
import logging
import cProfile
import pstats
import io

from functools import partial
from timeit import default_timer as timer

from aiostomp.aiostomp import AioStomp


DEFAULT_NUM_MSGS = 100000
DEFAULT_NUM_PUBS = 1
DEFAULT_NUM_SUBS = 0
DEFAULT_MESSAGE_SIZE = 128


def get_parameters(args):
    parser = argparse.ArgumentParser(description='AioStomp Benchmark')

    parser.add_argument(
        '-np',
        type=int,
        default=DEFAULT_NUM_PUBS,
        help="Number of publishers [default: %(default)s]")

    parser.add_argument(
        '-ns',
        type=int,
        default=DEFAULT_NUM_SUBS,
        help="Number os subscribers [default: %(default)s].")

    parser.add_argument(
        '-n',
        type=int,
        default=DEFAULT_NUM_MSGS,
        help="Number of messages to send [default: %(default)s].")

    parser.add_argument(
        '-ms',
        type=int,
        default=DEFAULT_MESSAGE_SIZE,
        help="Message size [default: %(default)s].")

    parser.add_argument(
        '-csv',
        type=str,
        help="Message size [default: %(default)s].")

    parser.add_argument(
        '--uvloop',
        default=False,
        action='store_true',
        help='Use uvloop [default: %(default)s].')

    parser.add_argument(
        '--profile',
        default=False,
        action='store_true',
        help='Enable profile [default: %(default)s].')

    parser.add_argument(
        'server',
        help="Stomp server address [127.0.0.1:61613].")

    parser.add_argument(
        'queue',
        help="Stomp queue to be used.")

    return parser.parse_args(args)


class InfoFilter(logging.Filter):
    def filter(self, rec):
        return rec.levelno in (logging.DEBUG, logging.INFO)


def logging_setup(level):
    log_level = level.upper()

    logger = logging.getLogger()
    logger.setLevel(log_level)

    logging_formatter = logging.Formatter(
        fmt='%(asctime)s %(name)s:%(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setLevel(log_level)
    stdout.addFilter(InfoFilter())
    stdout.setFormatter(logging_formatter)

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(logging.WARNING)
    stderr.setFormatter(logging_formatter)

    logger.addHandler(stdout)
    logger.addHandler(stderr)


def message_per_client(messages, clients):
    n = messages // clients

    messages_per_client = [n for x in range(clients)]

    remainder = messages % clients

    for x in range(remainder):
        messages_per_client[x] += 1

    return messages_per_client


def human_bytes(bytes_, si=False):
    base = 1024
    pre = ["K", "M", "G", "T", "P", "E"]
    post = "B"
    if si:
        base = 1000
        pre = ["k", "M", "G", "T", "P", "E"]
        post = "iB"

    if bytes_ < float(base):
        return "{:.2f} B".format(bytes_)

    exp = int(math.log(bytes_) / math.log(float(base)))
    index = exp - 1
    units = pre[index] + post
    return "{:.2f} {}".format(bytes_ / math.pow(float(base), float(exp)), units)


class Sample():
    def __init__(self, messages, msg_length, start, end):
        self.messages = messages
        self.msg_length = msg_length
        self.msg_bytes = messages * msg_length
        self.start = start
        self.end = end

    @property
    def rate(self):
        return float(self.messages) / self.duration

    @property
    def duration(self):
        return self.end - self.start

    @property
    def throughput(self):
        return self.msg_bytes / self.duration

    def __str__(self):
        return "{:.2f} msgs/sec ~ {}/sec".format(self.rate, human_bytes(self.throughput, si=False))


class SampleGroup(Sample):

    def __init__(self):
        super().__init__(0, 0, 0, 0)
        self.samples = []

    def add_sample(self, sample):
        self.samples.append(sample)

        if len(self.samples) == 1:
            self.start = sample.start
            self.end = sample.end

        self.messages += sample.messages
        self.msg_bytes += sample.msg_bytes

        if sample.start < self.start:
            self.start = sample.start

        if sample.end > self.end:
            self.end = sample.end

    def statistics(self):
        return "min {:.2f} | avg {:.2f} | max {:.2f} | stddev {:.2f} msgs".format(
            self.min_rate,
            self.avg_rate,
            self.max_rate,
            self.std_dev)

    @property
    def min_rate(self):
        for i, s in enumerate(self.samples):
            if i == 0:
                m = s.rate
            m = min(m, s.rate)
        return m

    @property
    def max_rate(self):
        for i, s in enumerate(self.samples):
            if i == 0:
                m = s.rate
            m = max(m, s.rate)
        return m

    @property
    def avg_rate(self):
        sum_ = 0
        for s in self.samples:
            sum_ += s.rate
        return sum_ / len(self.samples)

    @property
    def std_dev(self):
        avg = self.avg_rate
        sum_ = 0

        for c in self.samples:
            sum_ += math.pow(c.rate - avg, 2)

        variance = sum_ / len(self.samples)
        return math.sqrt(variance)


class Benchmark():

    def __init__(self, server):
        self.subscribe = SampleGroup()
        self.publish = SampleGroup()
        self.server = server

    def add_sample(self, sample_type, sample):
        if sample_type == 'subscribe':
            self.subscribe.add_sample(sample)
        elif sample_type == 'publish':
            self.publish.add_sample(sample)

    def report(self):
        print('== AioStomp Benchmark ==')
        print(' Testing against: {}'.format(self.server))

        if len(self.publish.samples):
            print('\n Publish:')
            for i, s in enumerate(self.publish.samples):
                print('  [{}] {} ({} msgs)'.format(i + 1, s, s.messages))

            print('  Totals:')
            print('   {} ({} msgs)'.format(
                self.publish, self.publish.messages))
            print('   {}'.format(self.publish.statistics()))

        if len(self.subscribe.samples):
            print('\n Subscribe')
            for i, s in enumerate(self.subscribe.samples):
                print('  [{}] {} ({} msgs)'.format(i + 1, s, s.messages))

            print('  Totals:')
            print('   {} ({} msgs)'.format(
                self.subscribe, self.subscribe.messages))
            print('   {}'.format(self.subscribe.statistics()))

    def to_csv(self):
        pass


async def create_connection(address, client_id):
    host, port = address.split(':')

    client = AioStomp(host, int(port), client_id=client_id)

    await client.connect()

    return client


async def run_publish(client, bench, message_size, num_msgs, queue):

    msg = ''.join([
        random.choice(string.printable)
        for n in range(message_size)])

    start = timer()
    for n in range(num_msgs):
        client.send(queue, msg)
        await asyncio.sleep(0)

    end = timer()
    bench.add_sample('publish', Sample(num_msgs, message_size, start, end))
    client.close()


async def run_subscribe(client, bench, message_size, num_msgs, queue):

    class Handler:
        def __init__(self, client, bench, message_size, num_msgs):
            self.counter = 0
            self.client = client
            self.bench = bench
            self.message_size = message_size
            self.num_msgs = num_msgs
            self.timeout = None
            self.start = None
            self.end = None

            self.loop = asyncio.get_event_loop()
            self._waiter = self.loop.create_future()
            self._task = asyncio.ensure_future(self.timeout_task())

        async def timeout_task(self):
            while True:
                time = self.loop.time()

                if self.timeout and time >= self.timeout:
                    self.bench.add_sample(
                        'subscribe',
                        Sample(self.counter, self.message_size, self.start, self.end))
                    self._waiter.set_result(True)

                await asyncio.sleep(0.2)

        def reset_timeout(self, timeout, end):
            self.timeout = timeout
            self.end = end

        async def handle_message(self, frame, message):
            self.counter += 1

            if self.start is None:
                self.start = timer()

            if self.timeout is None:
                self.timeout = self.loop.time() + 0.6
            else:
                cancel_at = self.loop.time() + 0.6
                self.reset_timeout(cancel_at, timer())

        async def wait_complete(self):
            await self._waiter

            self._task.cancel()
            self.client.close()

    h = Handler(client, bench, message_size, num_msgs)

    client.subscribe(queue, handler=partial(Handler.handle_message, h))

    await h.wait_complete()


async def run_benchmark(params):

    subscribers = []
    publishers = []

    if params.profile:
        pr = cProfile.Profile()
        pr.enable()

    bench = Benchmark(params.server)

    for s in range(params.ns):
        subscribers.append(create_connection(
            params.server, 'bench-sub#{}'.format(s)))

    subscribers = await asyncio.gather(*subscribers)

    for s in range(params.np):
        publishers.append(create_connection(
            params.server, 'bench-pub#{}'.format(s)))

    publishers = await asyncio.gather(*publishers)

    tasks = []
    if params.ns != 0:
        sub_messages = params.n // params.ns

    for i, client in enumerate(subscribers):
        tasks.append(
            run_subscribe(client,
                          bench,
                          params.ms,
                          sub_messages,
                          params.queue))

    if params.np != 0:
        pub_messages = message_per_client(params.n, params.np)

    for i, client in enumerate(publishers):
        tasks.append(
            run_publish(client, bench, params.ms, pub_messages[i], params.queue))

    await asyncio.gather(*tasks)

    if params.profile:
        pr.disable()

        s = io.StringIO()
        sortby = 'cumulative'
        ps = pstats.Stats(pr, stream=s).sort_stats(sortby)
        ps.print_stats()

    bench.report()

    if params.profile:
        print(s.getvalue())


def main(args=None):

    if args is None:
        args = sys.argv[1:]

    params = get_parameters(args)

    if not params.server or not params.queue:
        return

    logging_setup('debug')

    if params.uvloop:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

    loop = asyncio.get_event_loop()
    loop.run_until_complete(run_benchmark(params))


if __name__ == '__main__':
    main(sys.argv[1:])
