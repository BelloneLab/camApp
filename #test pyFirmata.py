#test pyFirmata
import pyfirmata
import time
from pyfirmata import Arduino, util
board = Arduino('COM11')
# Iterator pour buffer (essentiel pour lectures continues)
it = pyfirmata.util.Iterator(board)
it.start()
# Lire digital pin 2 (GPIO2)
pin2 = board.get_pin('d:3:o')  # d:digital, :i input
while True:
    print("GPIO3:", pin1.read())  # 0.0 ou 1.0
    time.sleep(0.1)
