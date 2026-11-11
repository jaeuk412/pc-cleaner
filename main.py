"""실행 진입점: python main.py <명령> ...   (python main.py --help 참고)"""
import sys

from pccleaner.cli import main

if __name__ == "__main__":
    sys.exit(main())
