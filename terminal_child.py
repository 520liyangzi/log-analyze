"""POSIX-only child: attach the controlling PTY before executing the user's shell."""
import os
import sys

if __name__ == '__main__':
    import fcntl
    import termios
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    shell = sys.argv[1]
    os.execv(shell, [shell, '-i'])
