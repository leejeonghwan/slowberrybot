# GitHub Pages 배포

이 프로젝트는 GitHub Pages용 정적 사이트를 GitHub Actions에서 직접 빌드해 배포합니다.

## 동작 방식

- `scripts/build_github_pages.py`가 Daum 트렌드를 수집합니다.
- 정적 사이트를 `site/` 디렉터리에 생성합니다.
- GitHub Actions가 `site/`를 GitHub Pages 아티팩트로 업로드하고 배포합니다.
- 스케줄은 매시 `17분`과 `47분`에 실행됩니다.

## 한 번만 할 설정

GitHub 저장소에서 다음을 확인하세요.

1. `Settings > Pages`
2. `Source`를 `GitHub Actions`로 선택

이 설정을 해야 [GitHub 공식 문서](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)에 나온 `deploy-pages` 워크플로우가 정상적으로 배포됩니다.

## 워크플로우 파일

- [deploy-github-pages.yml](/Users/leejeonghwan/Downloads/assembly-signal/.github/workflows/deploy-github-pages.yml)

## 로컬에서 미리 보기

온라인 수집 없이 샘플 JSON으로 정적 사이트를 생성할 수 있습니다.

```bash
python scripts/build_github_pages.py \
  --input-json tests/fixtures/trends_snapshot.json \
  --output-dir site
python3 -m http.server 8000 -d site
```

## 운영 메모

- 현재 브랜치 `codex-daum-trends-web-app`로 푸시하면 한 번 배포를 시도합니다.
- 주기 스케줄은 GitHub 기준으로 기본 브랜치에서 실행되므로, 자동 갱신은 `main` 병합 후 안정적으로 동작합니다.
- Pages 사이트는 정적이라서, 브라우저는 `./data/trends.json`만 다시 읽습니다.

