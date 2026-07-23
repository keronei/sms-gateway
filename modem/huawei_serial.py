import os,termios,select,time,threading
class HuaweiSerial:
    def __init__(self,port,baudrate=115200,timeout=1.0,debug=False):
        self.port=port; self.timeout=timeout; self.debug=debug; self.fd=None; self.lock=threading.RLock()
    def open(self):
        if self.fd is not None:return
        self.fd=os.open(self.port,os.O_RDWR|os.O_NOCTTY)
        attrs=termios.tcgetattr(self.fd)
        attrs[0]=0; attrs[1]=0; attrs[2]=termios.CS8|termios.CREAD|termios.CLOCAL|termios.HUPCL; attrs[3]=0
        termios.tcsetattr(self.fd,termios.TCSANOW,attrs)
    def close(self):
        if self.fd is not None: os.close(self.fd); self.fd=None
    @property
    def is_open(self): return self.fd is not None
    def __enter__(self): self.open(); return self
    def __exit__(self,*a): self.close()
    def write(self,data):
        with self.lock:
            if isinstance(data,str): data=data.encode()
            if self.debug: print("TX>",data)
            os.write(self.fd,data)
    def read(self,size=1024):
        r,_,_=select.select([self.fd],[],[],self.timeout)
        if not r:return b""
        d=os.read(self.fd,size)
        if self.debug: print("RX>",d)
        return d
    def readline(self):
        out=b""; end=time.time()+self.timeout
        while time.time()<end:
            b=self.read(1)
            if not b: continue
            out+=b
            if out.endswith(b"\n"): break
        return out
    def flush_input(self):
        while self.read(1024): pass
