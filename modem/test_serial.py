from modem import HuaweiSerial
import time
with HuaweiSerial('/dev/ttyUSB2',timeout=2,debug=True) as s:
    s.flush_input()
    for cmd in ['AT','ATI','AT+CSQ']:
        print('CMD',cmd)
        s.write(cmd+'\r')
        time.sleep(.2)
        print(s.read(1024).decode(errors='ignore'))
