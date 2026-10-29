# 번역 프롬프트와 OCR 검수 설계 (0.4.0)

2026-10-29에 아래 공개 프로젝트의 공식 문서와 실제 프롬프트를 확인했다.
공통 접근을 참고해 자체 한국어 지침을 작성했으며, 타 프로젝트의 프롬프트를 그대로 복제하지 않았다.

| 참고 구현 | 확인한 접근 | sub2loc에 적용한 판단 |
| --- | --- | --- |
| [Subtitle Edit advanced local engines](https://github.com/SubtitleEdit/subtitleedit/blob/main/docs/features/auto-translate-advanced.md) | 주변 대사, 이전 번역, synopsis, glossary, 말투와 줄바꿈 설정 | 고정 메모·근처 원문·번역 이력·요약을 구별하고, 말투·용어의 일관성을 구체적으로 명시 |
| [BallonsTranslator output contract](https://github.com/dmMaze/BallonsTranslator/blob/dev/ballontranslator/modules/translators/llm_translation_contract.py), [공식 문서](https://github.com/dmMaze/BallonsTranslator/blob/dev/README_EN.md) | ID/개수/출력 형식과 번역 스타일을 분리하고 glossary/history를 참고 자료로 전달 | 편집 가능한 스타일 템플릿과 앱의 ID·ASS 프로토콜을 분리하는 기존 구조 유지 |
| [manga-image-translator config_gpt.py](https://github.com/zyddnys/manga-image-translator/blob/main/manga_translator/translators/config_gpt.py) | 의미·감정·문화적 뉘앙스·말풍선 간 연결과 예시, 용어집을 다룸. 직역/분석/다듬기의 3단계 지침도 존재 | 의미·감정·문장 조각의 연결은 참고. 출력에 분석 과정을 요구하는 방식은 채택하지 않고 짧은 규칙과 최종 번역만 사용 |
| [Manga OCR 공식 usage tips](https://github.com/kha-white/manga-ocr#usage-tips) | 긴 영역 인식의 오류와 텍스트가 없는 이미지에서도 문장을 생성할 가능성을 설명 | OCR을 무조건 정답으로 보지 않고 이미지 미확인 후보를 별도 검수 |

## 기본 템플릿

- `sub2loc/templates/anime_ja_ko.txt`: 자막의 ID, 의미·부정·행동 주체, 말투, 호칭, 반복 농담, ASS 구조.
- `sub2loc/templates/manga_ja_ko.txt`: 말풍선별 정보 배분, 확인되지 않은 화자·읽기 순서, OCR 추정 교정과 번역의 분리.
- `sub2loc/templates/ocr_review_ja.txt`: 원문/같은 페이지 근처 블록을 보고 OCR 의심과 일본어 교정 후보를 생성. 확신도는 low/medium만 허용하며 이미지 확인으로 표현하지 않음.
- 기존 정확한 내장 v1 템플릿만 새 기본으로 갱신한다. 사용자 편집 템플릿은 보존한다. 기본 템플릿이 바뀌면 기존 프롬프트로 만든 번역 캐시와 분리될 수 있다.
- 자동 기본 템플릿은 벌룬 TXT일 때 만화용으로 바뀐다. 상세 번역 설정의 애니/만화 기본 버튼으로 명시적으로 적용할 수 있다.

문장을 하드코딩해 치환하지 않는다. 특히 실제 작품의 오역 예문을 강제로 치환하지 않는다.
원문 지시문은 데이터로 취급하고, 스타일 설정은 ID/출력 규약을 바꿀 수 없다.
이 지침이 실제 Gemma 번역 품질을 향상한다는 수치는 아직 없다. 모의 API/구조 테스트와 실제 로컬 모델 품질 평가는 구별한다.

## OCR 검수 흐름

1. 기본 번역 후 원문 TXT 하나를 선택하고 `벌룬 OCR 검수`를 연다. 이전 버전에서 번역했으면 같은 원문의 번역 TXT를 직접 불러온다.
2. 기본 반복·깨진 문자 검사와 기존 unresolved 검수 항목을 표시한다. `모델로 전체 OCR 검수`는 별도의 추가 API 작업이다. 최대 20블록과 같은 페이지의 인접 블록을 묶고 입력 예산을 확인한다.
3. PolyStrata는 이 검수와 후보 생성에만 Thinking Low가 기본이다. OFF/Medium 선택 가능. 일반 OpenAI 호환 제공자에는 비표준 Thinking 옵션을 보내지 않는다. 본 번역·애니 오류 재번역·요약 설정은 바꾸지 않는다.
4. `그대로 읽기`는 원문 유지 결정을 저장한다. 의심 표시는 지우지 않는다. `선택 블록 교정 후보 만들기` 또는 직접 입력으로 일본어 후보를 준비한다.
5. `후보로 다시 번역`은 선택한 한 블록만 요청하고 ID·형식·문자·반복·길이를 검사한다. 다른 블록은 재요청하지 않는다.
6. 미리보기를 확인하고 `이 번역 적용 · 새 TXT 저장`을 누르면 기존 파일을 덮어쓰지 않는 사본을 만든다. 이 파일을 벌룬에 가져온다. `.ocr-review.json`에는 원문 해시, 페이지/블록별 의심, 후보, 이전/새 번역, 사용자 결정과 `image_verified: false`를 기록한다.

검수 상태는 EXE 옆 `data/ocr-review`에 원문 경로+내용 해시별로 보관한다. 다른 권/다른 원문 파일과 혼합하지 않는다. 원문이나 불러온 번역 파일이 외부에서 변경되면 적용을 거부한다. 중단/실패/잘린 응답/중복·미등록 ID는 원문과 기존 번역을 변경하지 않는다. 이미 완료된 검수 묶음은 저장한다.

OCR 수정본은 별도 결과이며 기존 자동 번역 캐시와 요약을 소급 수정하지 않는다. 나중에 전체 번역을 다시 실행한 결과보다 OCR 수정본을 따로 보관해야 한다. 아직 이미지 재인식·자동 영역 복구·정확한 OCR 확신도·누락된 말풍선 탐지는 제공하지 않는다. 원문만 보고 만든 그럴듯한 교정은 의미를 바꿀 수 있으므로 자동 적용하지 않는다.

## Windows 로컬 확인

1. PolyStrata 실행, API 주소와 모델 확인 후 TXT를 50개씩 번역한다.
2. 원문 TXT 선택 → OCR 검수. 애니/일반 번역은 OFF로 두고 OCR 검수만 Low로 실행한다. 서버에서 해당 검수 요청에 Thinking이 생기는지 확인한다.
3. 의도된 효과음 반복은 `그대로 읽기`로 남긴다. 잘못된 단어는 같은 페이지 문맥으로 후보를 생성하고 필요하면 직접 수정한다.
4. 한 블록 재번역·미리보기·새 사본 저장 후 페이지/블록 구조와 나머지 번역이 유지됐는지, 벌룬 가져오기와 실제 화면 표시를 확인한다.
5. 취소, 서버 중지, 짧은 응답 한도도 확인한다. 모델 검수에서 문제를 찾지 못해도 정확성이 입증된 것은 아니다.

클라우드 환경에서 사용자 PC의 localhost, 실제 Gemma Thinking 효과, 벌룬의 이미지 배치는 검증할 수 없다. 테스트는 모의 HTTP와 네이티브 UI로 요청 옵션·선택 블록만의 적용·보존·오류 동작을 확인한다.
