"""`python -m pccleaner`로 패키지를 직접 실행할 때의 진입점(Entry Point) 모듈."""
import sys

from .cli import main

# CLI 메인 함수를 실행하고 반환된 종료 코드(exit code)를 시스템에 전달합니다.
sys.exit(main())
