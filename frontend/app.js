const chatBox = document.getElementById("chat-box");
const userInput = document.getElementById("user-input");
const sendButton = document.getElementById("send-button");

function addMessage(content, sender, isHtml = false) {
  const div = document.createElement("div");
  div.className = `message ${sender}`;

  if (isHtml) {
    div.innerHTML = content;
  } else {
    div.textContent = content;
  }

  chatBox.appendChild(div);
  chatBox.scrollTop = chatBox.scrollHeight;
}

async function sendMessage() {
  const text = userInput.value.trim();

  if (!text) {
    return;
  }

  addMessage(text, "user");
  userInput.value = "";

  sendButton.disabled = true;
  sendButton.textContent = "조회 중...";

  try {
    const response = await fetch("/api/chat_query", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        message: text
      })
    });

    const data = await response.json();

    if (!response.ok) {
      addMessage(
        data.answer_html || data.answer || data.error || "API 호출 중 오류가 발생했습니다.",
        "bot",
        Boolean(data.answer_html)
      );
      return;
    }

    addMessage(
      data.answer_html || data.answer || "응답이 비어 있습니다.",
      "bot",
      Boolean(data.answer_html)
    );

  } catch (error) {
    console.error(error);

    addMessage(
      "백엔드 API 호출에 실패했습니다. Azure Static Web Apps와 Azure Function 연결 상태를 확인하세요.",
      "bot"
    );

  } finally {
    sendButton.disabled = false;
    sendButton.textContent = "전송";
    userInput.focus();
  }
}

sendButton.addEventListener("click", sendMessage);

userInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    sendMessage();
  }
});