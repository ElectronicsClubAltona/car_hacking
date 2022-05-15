import asyncio
import can
import isotp
import time
import threading
import logging
import dictdiffer

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)-15s %(levelname)-8s:%(name)-12s:%(message)s",
    filename="bmw_gws_uds.debug.log",
)


# Reference https://gist.github.com/brandonros/4aa6ae51d0f925671d034446947df555

def hard_reset_simple(bus):
    # arbitration ID encodes sender '0xf1' (tester), destination is in first data byte - 5e for GWS, can also use 0xdf for broadcast
    # then send 2 byte UDS payload 0x11 0x01. response comes via arbitration ID 0x65
    broadcast_msg = can.Message(arbitration_id=0x6f1, data=b'\x5e\x02\x11\x01', is_extended_id=False)
    bus.send(broadcast_msg)
    t0 = time.time()
    while time.time() < t0 + 1.0:
        r = bus.recv(0.1)
        if r and 0x600 <= r.arbitration_id < 0x700:
            print(r)


def hard_reset(bus):
    return req_isotp(bus, b'\x11\x01')

def req_isotp(bus, req):
        with ThreadedBmwIsoTp(bus, 0x5e, 0xf1) as iso:
            r = iso.request(req, timeout=0.5)
            return r

def get_dtcs(bus, status_mask=0x0c):
    # status mask 0x0c seems to be 'active'
    for tries in range(3):
        r = req_isotp(bus, [0x19, 0x02, status_mask])
        if r is not None:
            return decode_dtcdata(r)

def get_supported_dtcs(bus):
    return decode_dtcdata(req_isotp(bus, [0x19, 0x0a]))


def decode_dtcdata(dtcdata):
    if dtcdata[0] != 0x59:
        raise RuntimeError(f'Unexpected response tag {dtcdata[0]:#x}')
    if (len(dtcdata) - 3) % 4 != 0:
        raise RuntimeError(f'Unexpected response length {len(dtcdata)}')
    num_dtcs = (len(dtcdata) - 3) // 4
    dtcs = {}
    for i in range(num_dtcs):
        offs = 3 + i*4
        dtc = dtcdata[offs:offs+3]
        status = dtcdata[offs+3]
        # status 0x2f = active, persistent & current, I think
        # status 0x6d = stored? not sure about this one
        # status 0x2c = active, current but goes to 0x2f after some time
        dtcs[dtc.hex()] = status
    return dtcs


def try_message(bus):
    for base in range(255):
        print(f'Base bytes {base:#x}')
        for offs in range(8):
            print(f'Changing offset {offs}')
            for byte in range(255):
                payload = [ base ] * 8
                payload[offs] = byte
                message = can.Message(arbitration_id=0x3fd, data=payload, is_extended_id=False)
                for _ in range(16):
                    bus.send(message)
                    time.sleep(0.01)

                time.sleep(0.1)

                dtcs = get_dtcs(bus)

                csum_dtc = dtcs.get('e09404', 'missing')
                if csum_dtc != 47:
                    print(f'Message {message}')
                    print(f'   E09404 -> {csum_dtc}')


def simple_query(bus, send_data):
    txid = 0x7ca
    rxid = 0x7c9

    msg = can.Message(arbitration_id=txid, is_extended_id=False, data=send_data)
    bus.send(msg)
    t0 = time.time()
    while time.time() < t0 + 0.2:
        r = bus.recv(0.1)
        if r and r.arbitration_id == rxid:
            return(r.data)
    return None


class ThreadedBmwIsoTp:
    def __init__(self, bus, target_address, source_address):
        assert target_address < 0x100
        assert source_address < 0x100
        self.exit_requested = False
        self.bus = bus
        self.rxid = 0x600 | target_address
        addr = isotp.Address(
            isotp.AddressingMode.Extended_11bits,
            rxid=0x600 | target_address,
            txid=0x600 | source_address,
            target_address=target_address,
            source_address=source_address
        )
        self.stack = isotp.CanStack(
            self.bus,
            address=addr,
            error_handler=self.my_error_handler,
            params=isotp_params,
        )

    def __enter__(self):
        self.old_filters = self.bus.filters
        self.bus.filters = [{"can_id": self.rxid, "can_mask": 0xFFFFFFF}]
        self.start()
        return self

    def __exit__(self, type, value, tb):
        self.stop()
        self.bus.filters = self.old_filters

    def start(self):
        self.exit_requested = False
        self.thread = threading.Thread(target=self.thread_task)
        self.thread.start()

    def stop(self):
        self.exit_requested = True
        if self.thread.is_alive():
            self.thread.join()

    def my_error_handler(self, error):
        logging.warning(
            "IsoTp error happened : %s - %s" % (error.__class__.__name__, str(error))
        )

    def thread_task_disabled(self):
        import cProfile

        cProfile.runctx(
            "self.thread_task_()", globals=globals(), locals=locals(), sort="cumtime"
        )

    def thread_task(self):
        while self.exit_requested == False:
            self.stack.process()  # Non-blocking
            # (sleeping here seems to cause the diagnostic session to time out
            # time.sleep(0.001)
            # time.sleep(self.stack.sleep_time()) # Variable sleep time based on state machine state

    def shutdown(self):
        self.stop()
        self.bus.shutdown()

    def request(self, send_bytes, timeout=1.0):
        self.stack.send(send_bytes)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.stack.available():
                return self.stack.recv()
            time.sleep(0.05)
        #print(f"Timeout after {time.time() - t0:.1f}s")
        return None


isotp_params = {
    # Will request the sender to wait 32ms between consecutive frame. 0-127ms or 100-900ns with values from 0xF1-0xF9
    "stmin": 1,
    # Request the sender to send 8 consecutives frames before sending a new flow control message
    "blocksize": 0,
    # Number of wait frame allowed before triggering an error
    "wftmax": 0,
    # Link layer (CAN layer) works with 8 byte payload (CAN 2.0)
    "ll_data_length": 8,
    # Will pad all transmitted CAN messages with byte 0x00. None means no padding
    "tx_padding": 0,
    # Triggers a timeout if a flow control is awaited for more than 1000 milliseconds
    "rx_flowcontrol_timeout": 500,
    # Triggers a timeout if a consecutive frame is awaited for more than 1000 millisecondsa
    "rx_consecutive_frame_timeout": 1000,
    # When sending, respect the stmin requirement of the receiver. If set to True, go as fast as possible.
    "squash_stmin_requirement": False,
}