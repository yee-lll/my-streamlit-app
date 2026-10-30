
import os
import streamlit as st
from openai import OpenAI

# OpenAI 클라이언트 생성
client = OpenAI()

# 페이지 설정
st.set_page_config(page_title="OpenAI 챗봇", page_icon="🤖")
st.title("🤖 OpenAI API 챗봇")
st.write("Streamlit + OpenAI API 실습 예제")

#세션 상태 초기화
if "messages" not in st.session_state:
  st.session_state.messages = [{"role":"assistant",
                                "content":"안녕하세요. 무엇을 도와드릴까요?"}]

#기존 대화 출력
for message in st.session_state.messages:
  with st.chat_message(message["role"]):
    st.markdown(message["content"])

#사용자 입력
user_input = st.chat_input("메세지를 입력하세요...")


# # 사용자 질문
# prompt = st.text_input("질문을 입력하세요")

# # 질문 버튼
# if st.button("질문하기"):
#     if prompt.strip() == "":
#         st.warning("질문을 입력하세요")
#     else:
#          with st.spinner("답변 생성 중입니다..."):
#             # LLM 호출
#             response = client.responses.create(
#                 model="gpt-4o-mini",
#                 input=[{"role": "user", "content": prompt}]
#             )
#             answer = response.output_text

#             # 결과 출력
#             st.subheader("AI 답변")
#             st.write(answer)
