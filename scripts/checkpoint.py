"""Run phase checks and save a resumable checkpoint only after they pass."""
import argparse
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', type=int)
    parser.add_argument('description')
    parser.add_argument('next_task')
    parser.add_argument('tests', nargs='+')
    args = parser.parse_args()
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', *args.tests],
                            capture_output=True, text=True)
    print(result.stdout)
    print(result.stderr)
    path = Path('docs/IMPLEMENTATION_STATUS.md')
    text = path.read_text(encoding='utf-8')
    lines = text.splitlines()
    replacements = {
        '- 현재 실패 중인 테스트:': '없음' if result.returncode == 0 else result.stdout.strip().splitlines()[-1],
        '- 다음 작업:': args.next_task if result.returncode == 0 else f'Phase {args.phase} 테스트 실패 수정',
    }
    if result.returncode == 0:
        replacements.update({
            '- 마지막으로 완료된 Phase:': str(args.phase),
            '- 현재 진행 중인 Phase:': str(args.phase + 1) if args.phase < 25 else '없음 — 구현 완료',
            '- 완료된 기능:': f'Phase 1~{args.phase} (아래 검증 기록 참조)',
            '- 아직 구현하지 않은 기능:': f'Phase {args.phase + 1}~25' if args.phase < 25 else '없음. 실 API 운영 검증은 자격증명 필요.',
        })
    for i, line in enumerate(lines):
        for prefix, value in replacements.items():
            if line.startswith(prefix):
                lines[i] = f'{prefix} {value}'
    text = '\n'.join(lines) + '\n'
    summary = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else 'no output'
    record = f'- Phase {args.phase}: {args.description}; `{summary}`; 다음: {args.next_task}\n'
    text = text.replace('## 설계 결정 / 미완성 코드', record + '\n## 설계 결정 / 미완성 코드')
    path.write_text(text, encoding='utf-8', newline='\n')
    sys.exit(result.returncode)


if __name__ == '__main__':
    main()
