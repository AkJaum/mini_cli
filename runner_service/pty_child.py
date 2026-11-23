"""Attach the child session to its terminal before replacing it with the command."""

import fcntl
import os
import sys
import termios


def main():
    fcntl.ioctl(sys.stdin.fileno(), termios.TIOCSCTTY, 0)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
