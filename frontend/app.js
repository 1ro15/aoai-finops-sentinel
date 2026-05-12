const chatBox = document.getElementById("chat-box");
const userInput = document.getElementById("user-input");
const sendButton = document.getElementById("send-button");

let lastReportQuery = "";

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

function isMailOnlyRequest(text) {
  const lower = text.toLowerCase();
  const hasMailWord =
    lower.includes("메일") ||
    lower.includes("이메일") ||
    lower.includes("mail") ||
    lower.includes("email");

  const hasDateHint =
    text.includes("월") ||
    text.includes("/") ||
    text.includes("-") ||
    /\d{4}/.test(text);

  return hasMailWord && !hasDateHint;
}

async function sendMessage() {
  let text = userInput.value.trim();

  if (!text) {
    return;
  }

  addMessage(text, "user");
  userInput.value = "";

  if (isMailOnlyRequest(text)) {
    if (!lastReportQuery) {
      addMessage(
        "메일로 보낼 이전 조회 결과가 없습니다. 먼저 기간을 지정해서 사용량을 조회해주세요.",
        "bot"
      );
      return;
    }

    text = `${lastReportQuery} 메일로 보내줘`;
  }

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

    if (!isMailOnlyRequest(text) && !text.includes("메일") && !text.includes("이메일")) {
      lastReportQuery = text;
    }

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