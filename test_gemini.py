"""
Gemini API 키 작동 확인용 스크립트

사용법:
    python test_gemini.py
환경변수 GEMINI_API_KEY 에 저장된 키를 읽어서 짧은 질문을 한 번 보냅니다.
"""
import os
import sys

MODEL = "gemini-3.8-flash"  # gemini-2.5-flash는 신규 사용자에게 막혀 있음

# 1. 키가 환경변수에 있는지 확인 (키 전체는 출력하지 않음)
key = os.environ.get("GEMINI_API_KEY")
if not key:
    print("❌ GEMINI_API_KEY 환경변수를 찾을 수 없습니다.")
    print("   setx 로 저장했다면 VSCode를 완전히 껐다가 다시 켜세요.")
    sys.exit(1)
print(f"🔑 키 확인: {key[:6]}...{key[-4:]} (길이 {len(key)})")

# 2. 라이브러리 확인
try:
    from google import genai
except ImportError:
    print("❌ google-genai 라이브러리가 없습니다. 먼저 설치하세요:")
    print("   python -m pip install google-genai")
    sys.exit(1)

# 3. 실제 호출
client = genai.Client(api_key=key)
try:
    response = client.models.generate_content(
        model=MODEL,
        contents="F1에서 언더컷이 뭔지 한 문장으로 설명해줘",
    )
    print(f"✅ 성공! ({MODEL}) 응답:")
    print(response.text)
except Exception as e:
    msg = str(e)
    print(f"❌ 호출 실패: {type(e).__name__}")
    print(f"   {msg[:300]}")
    if "API key not valid" in msg or "PERMISSION_DENIED" in msg or "403" in msg:
        print("👉 키가 잘못됐습니다. AI Studio에서 키를 다시 복사해 저장하세요 (앞뒤 공백 주의).")
    elif "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        print("👉 키는 정상이지만 무료 한도를 넘었습니다. 잠시 뒤 다시 시도하세요.")
    elif "404" in msg or "NOT_FOUND" in msg:
        print(f"👉 키는 정상이지만 모델 이름({MODEL})이 없습니다. 사용 가능한 flash 모델:")
        for m in client.models.list():
            if "flash" in m.name:
                print("   -", m.name)
        print("   파일 위쪽 MODEL 값을 위 이름 중 하나로 바꾸세요.")
    sys.exit(1)
