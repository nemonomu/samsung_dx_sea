"""
공통 설정 모듈
- 작업 디렉토리 설정
- Windows 콘솔 한글 출력 설정
- 경로 설정

모든 크롤러에서 import하여 사용:
    from common.setup import setup_environment
    setup_environment(__file__)
"""

import sys
import os


def setup_environment(script_file):
    """
    크롤러 실행 환경 설정

    Args:
        script_file (str): 실행 중인 스크립트의 __file__ 값

    기능:
        1. 작업 디렉토리를 프로젝트 루트로 설정
        2. Windows 콘솔 한글 출력 설정
        3. 프로젝트 루트를 sys.path에 추가

    사용 예시:
        from common.setup import setup_environment
        setup_environment(__file__)
    """
    # 1. 작업 디렉토리 설정 (setup.py 자체 위치 기준 — 호출 스크립트 depth 무관)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(project_root)  # 작업 디렉토리 변경

    # 2. Windows 콘솔 한글 출력 설정
    if sys.platform == 'win32':
        import io
        # stdout/stderr가 아직 래핑되지 않았을 때만 래핑
        if not isinstance(sys.stdout, io.TextIOWrapper) or sys.stdout.encoding != 'utf-8':
            try:
                sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
            except (AttributeError, ValueError):
                pass
        if not isinstance(sys.stderr, io.TextIOWrapper) or sys.stderr.encoding != 'utf-8':
            try:
                sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)
            except (AttributeError, ValueError):
                pass

    # 3. 프로젝트 루트를 sys.path에 추가 (중복 방지)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    return project_root


def get_input_with_timeout(prompt="입력: ", default="0", timeout=10.0):
    """
    Windows 환경에서 지정된 시간(초) 내에 입력을 받지 못하면 기본값을 반환하는 함수
    """
    import sys

    # 터미널 환경이 아니면 바로 기본값 반환
    if not sys.stdin or not sys.stdin.isatty():
        return default

    if sys.platform == 'win32':
        import msvcrt
        import time

        print(prompt, end='', flush=True)
        start_time = time.time()
        input_str = ""
        while True:
            if msvcrt.kbhit():
                char = msvcrt.getwche()
                if char in ('\r', '\n'):
                    print()
                    return input_str if input_str else default
                elif char == '\x08': # 백스페이스
                    if len(input_str) > 0:
                        input_str = input_str[:-1]
                        sys.stdout.write('\b \b')
                        sys.stdout.flush()
                else:
                    input_str += char

            if time.time() - start_time > timeout:
                print(f"\n[입력 시간 초과] 기본값({default})으로 진행합니다.")
                return default
            time.sleep(0.05)
    else:
        # non-Windows (fallback)
        try:
            val = input(prompt)
            return val if val else default
        except Exception:
            return default