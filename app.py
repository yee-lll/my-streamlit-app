
import os
import streamlit as st
from openai import OpenAI

# OpenAI 클라이언트 생성
client = OpenAI()

# 페이지 설정
st.set_page_config(page_title="한글 → 영어 번역기", page_icon="🤖")
st.title("🤖 한글 → 영어 번역기")
st.write("입력한 한글 문장을 자연스러운 영어로 번역합니다.")

# 사용자 입력(텍스트)
korean_text = st.text_area(
    "번역할 한글 문장을 입력하세요.",
    height=150,
    placeholder="예: 인공지능은 우리의 일상과 업무 방식을 빠르게 변화시키고 있습니다."
)

# 번역 버튼
if st.button("번역하기"):

    if not korean_text.strip():
        st.warning("번역할 한글 문장을 입력하세요.")
    else:
        with st.spinner("번역 중입니다..."):

            # 시스템 역할
            sys_role = """
            당신은 한국어를 자연스러운 영어로 번역하는 전문 번역가입니다.
            의미를 유지하면서 원어민이 쓰는 표현으로 번역합니다.
            """

            # 프롬프트 구성
            prompt = f"""
            다음 문장을 영어로 번역하세요.
            TEXT:
            {korean_text}
            """

            # GPT 호출
            response = client.responses.create(
                model="gpt-4o-mini",
                input=[
                    {"role": "system", "content": sys_role},
                    {"role": "user", "content": prompt}
                ]
            )

            # 결과 출력
            translated_text = response.output_text

            st.subheader("번역 결과")
            st.success(translated_text)




# %%writefile app.py

# import os
# import base64
# import streamlit as st
# from openai import OpenAI

# # OpenAI 클라이언트 생성
# client =

# # 페이지 설정
# st.set_page_config(     )
# st.title(     )

# # 텍스트 입력
# text = st.text_area(
#     "읽을 텍스트를 입력하세요",
#     "안녕하세요. OpenAI 텍스트 음성 변환 실습입니다."
# )

# # 버튼
# if st.button(      ):
#     if text.strip() == "":
#         st.warning(      )
#     else:
#         # TTS 생성
#         speech = client.audio.speech.create(
#             model=     ,
#             voice=     ,
#             input=
#         )
#         audio_bytes =

#         # 음성 출력
#         st.markdown("### 🔊 음성 재생")
#         st.audio(speech.content, format="audio/mp3")
