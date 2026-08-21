"use strict";

const EXAMPLE_PROMPTS = {
  family:
    "HaYarkon 120, Tel Aviv, 4 rooms, 90 sqm, asking 3,200,000 ILS. The listing says " +
    "it's close to parks and schools. Is this a good fit for a family with two young kids?",
  price:
    "HaYarkon 120, Tel Aviv, 3 rooms, 80 sqm, asking 4,000,000 ILS for purchase. Is the " +
    "asking price reasonable compared to the area?",
  rental:
    "HaYarkon 120, Tel Aviv, 3 rooms, 80 sqm, 6,000 ILS per month for rent. Is this rental " +
    "listing reasonable and what should I check before signing?",
};

const form = document.getElementById("triage-form");
const promptField = document.getElementById("prompt");
const runButton = document.getElementById("run-button");
const loadingIndicator = document.getElementById("loading");
const errorBox = document.getElementById("error");
const resultSection = document.getElementById("result");
const responseText = document.getElementById("response-text");
const stepsList = document.getElementById("steps-list");
const stepsDetails = document.getElementById("steps-details");
const conversationIndicator = document.getElementById("conversation-indicator");
const newConversationButton = document.getElementById("new-conversation-button");

// One conversation_id per active browser tab/conversation (Phase 17):
// enables stateful follow-up questions about the same listing without any
// login/session infrastructure. Cleared by "New conversation" or when the
// tab closes (sessionStorage) - never sent at all until the buyer has
// actually run at least one turn.
const CONVERSATION_STORAGE_KEY = "listingAgentConversationId";

function getConversationId() {
  return sessionStorage.getItem(CONVERSATION_STORAGE_KEY);
}

function ensureConversationId() {
  let id = getConversationId();
  if (!id) {
    id = (crypto.randomUUID && crypto.randomUUID()) || `conv-${Date.now()}-${Math.random()}`;
    sessionStorage.setItem(CONVERSATION_STORAGE_KEY, id);
  }
  return id;
}

function renderConversationIndicator() {
  const id = getConversationId();
  conversationIndicator.textContent = id
    ? `Conversation: ${id.slice(0, 8)}\u2026`
    : "New conversation (not started yet)";
}

newConversationButton.addEventListener("click", () => {
  sessionStorage.removeItem(CONVERSATION_STORAGE_KEY);
  renderConversationIndicator();
  resetOutput();
});

renderConversationIndicator();

document.querySelectorAll(".example-btn").forEach((button) => {
  button.addEventListener("click", () => {
    const key = button.dataset.example;
    if (key in EXAMPLE_PROMPTS) {
      promptField.value = EXAMPLE_PROMPTS[key];
      promptField.focus();
    }
  });
});

function resetOutput() {
  errorBox.hidden = true;
  errorBox.textContent = "";
  resultSection.hidden = true;
  responseText.textContent = "";
  stepsList.innerHTML = "";
  stepsDetails.open = false;
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
}

function renderSteps(steps) {
  stepsList.innerHTML = "";
  for (const step of steps || []) {
    const item = document.createElement("li");
    item.className = "step-item";

    const heading = document.createElement("h3");
    heading.textContent = `${step.step} (${step.status})`;
    item.appendChild(heading);

    const detail = step.detail || {};
    const promptDetail = detail.prompt || {};

    const promptLabel = document.createElement("strong");
    promptLabel.textContent = "Prompt:";
    item.appendChild(promptLabel);

    const promptPre = document.createElement("pre");
    promptPre.textContent = JSON.stringify(promptDetail, null, 2);
    item.appendChild(promptPre);

    const responseLabel = document.createElement("strong");
    responseLabel.textContent = "Response:";
    item.appendChild(responseLabel);

    const responsePre = document.createElement("pre");
    responsePre.textContent = JSON.stringify(
      "response" in detail ? detail.response : (detail.error ?? null),
      null,
      2
    );
    item.appendChild(responsePre);

    stepsList.appendChild(item);
  }
}

async function runAgent(prompt) {
  const conversationId = ensureConversationId();
  const response = await fetch("/api/execute", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, conversation_id: conversationId }),
  });

  let body;
  try {
    body = await response.json();
  } catch (parseError) {
    throw new Error("The agent returned an unexpected response. Please try again.");
  }

  if (!response.ok || body.status === "error") {
    throw new Error(body.error || "The agent could not complete this request.");
  }

  return body;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  resetOutput();

  const prompt = promptField.value.trim();
  if (!prompt) {
    showError("Please enter the listing details (and optional question) first.");
    return;
  }

  runButton.disabled = true;
  loadingIndicator.hidden = false;

  try {
    const body = await runAgent(prompt);
    responseText.textContent = body.response;
    renderSteps(body.steps);
    resultSection.hidden = false;
    renderConversationIndicator();
  } catch (err) {
    showError(err.message || "Something went wrong. Please try again.");
  } finally {
    runButton.disabled = false;
    loadingIndicator.hidden = true;
  }
});
