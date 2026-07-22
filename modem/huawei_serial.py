from serial.serialposix import Serial

class HuaweiSerial(Serial):

    def _update_dtr_state(self):
        try:
            super()._update_dtr_state()
        except BrokenPipeError:
            # Huawei option driver rejects DTR
            pass

    def _update_rts_state(self):
        try:
            super()._update_rts_state()
        except BrokenPipeError:
            pass