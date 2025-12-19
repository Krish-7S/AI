

const express = require("express");
const { twiml: { VoiceResponse } } = require("twilio");
const axios = require("axios");
const Groq = require("groq-sdk");

const app = express();
app.use(express.urlencoded({ extended: false }));

// ---------- This is CONFIG ----------
const FRESHDESK_DOMAIN = "xxxxxxxxxxxx";
const FRESHDESK_API_KEY = "xxxxxxxxxx"; 
const ZOOM_PHONE_DID = "+1xxxxxxx";
const GROQ_API_KEY = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"; 
const PORT = 3001;
// ---------------------------

const groq = new Groq({ apiKey: GROQ_API_KEY });
const callState = {};

function fdAuth() {
  return { auth: { username: FRESHDESK_API_KEY, password: "X" } };
}

// ==================== INCOMING CALL ====================
app.post("/voice", async (req, res) => {
  const callSid = req.body.CallSid;
  const from = req.body.From || "unknown";
  console.log(`\n NEW CALL | CallSid: ${callSid} | From: ${from}`);

  let contact = await findContactByPhone(from);
  if (!contact) contact = await createContact(from);

  let tickets = contact?.id ? await getRecentTicketsForContact(contact.id, 2) : [];

  callState[callSid] = {
    contact,
    tickets,
    ticketIndex: 0,
    silenceCount: 0,
    currentStage: "asked-before",
    lastAiAnswer: "",
    lastKbText: "", 
    kbAttempts: 0,
    webSearched: false,
    communitySearched: false,
    userDescription: "",
    newTicketId: null,
    askedForDetails: false,
  };

  console.log(`Contact ID: ${contact.id} | Recent tickets: ${tickets.length}`);

  const vr = new VoiceResponse();
  vr.say("Hi there! Welcome to Sandeza support. This is Luna speaking.");
  const gather = vr.gather({
    input: "speech",
    action: "/asked-before",
    timeout: 12,
    speechTimeout: "auto",
    hints: "yes,no,yeah,nope,wait,hold on,stop",
    speechModel: "phone_call",
  });
  gather.say("Have you already talked to one of our agents about this issue? Just say yes or no.");
  vr.redirect("/timeout");

  res.type("text/xml");
  res.send(vr.toString());
  console.log("→ Asked if they spoke to an agent before");
});

// ==================== TIMEOUT / SILENCE ====================
app.post("/timeout", (req, res) => {
  const callSid = req.body.CallSid;
  const state = callState[callSid] || {};
  state.silenceCount = (state.silenceCount || 0) + 1;
  callState[callSid] = state;

  console.log(` SILENCE #${state.silenceCount} | Stage: ${state.currentStage}`);

  if (state.silenceCount >= 3) {
    console.log(" Call ended due to too many silences");
    const vr = new VoiceResponse();
    vr.say("Hmm, seems like we got disconnected. Feel free to call back anytime. Take care!");
    res.type("text/xml");
    res.send(vr.toString());
    return;
  }

  const vr = new VoiceResponse();
  if (state.silenceCount === 2) {
    vr.say("Hello? Are you still with me?");
  } else {
    vr.say("No worries, take your time...");
  }

  let prompt = "", action = "", hints = "";
  switch (state.currentStage) {
    case "asked-before":
      prompt = "Just checking — have you already spoken with an agent about this? Yes or no?";
      action = "/asked-before"; hints = "yes,no,wait";
      break;
    case "confirm-ticket":
      const t = state.tickets?.[state.ticketIndex];
      prompt = t ? `Is your issue about: ${t.subject}?` : "Is this your issue?";
      action = "/confirm-ticket"; hints = "yes,no,wait";
      break;
    case "new-issue":
      prompt = "Whenever you're ready, tell me what's going on.";
      action = "/new-issue"; hints = "describe,issue,problem,wait";
      break;
    case "after-steps":
      prompt = "Did those steps help? Or are you still stuck?";
      action = "/after-steps"; hints = "yes,no,repeat,agent,wait";
      break;
    default:
      prompt = "Let’s try again — how can I help you today?";
      action = "/voice";
  }

  vr.say(prompt);
  vr.gather({ input: "speech", action, timeout: 12, speechTimeout: "auto", hints, speechModel: "phone_call" });
  vr.redirect("/timeout");

  res.type("text/xml");
  res.send(vr.toString());
});

// ==================== user needs times to explain so it must wait for a while ====================
function handleUserPause(req, res, redirectPath) {
  const vr = new VoiceResponse();
  vr.say("No rush at all, take your time.");
  vr.pause({ length: 12 });
  vr.say("Whenever you're ready, just say something — shall we continue?");
  vr.redirect(redirectPath);
  res.type("text/xml");
  res.send(vr.toString());
}

// ==================== Asked before ====================
app.post("/asked-before", (req, res) => {
  const callSid = req.body.CallSid;
  const speech = (req.body.SpeechResult || "").toLowerCase().trim();
  const state = callState[callSid];
  state.silenceCount = 0;
  console.log(` User said: "${speech}"`);

  if (speech.includes("wait") || speech.includes("hold") || speech.includes("stop") || speech.includes("one sec")) {
    console.log(" User asked to pause");
    return handleUserPause(req, res, "/asked-before");
  }

  const vr = new VoiceResponse();
  const saidYes = speech.includes("yes") || speech.includes("yeah");

  if (saidYes && state.tickets?.length > 0) {
    state.currentStage = "confirm-ticket";
    const ticket = state.tickets[0];
    console.log(` Offering ticket: "${ticket.subject}"`);
    vr.say(`Got it. Let me check if this matches your previous ticket...`);
    vr.pause({ length: 2 });
    const g = vr.gather({ input: "speech", action: "/confirm-ticket", timeout: 12, hints: "yes,no,wait,stop" });
    g.say(`Is your issue something like: ${ticket.subject}?`);
  } else {
    state.currentStage = "new-issue";
    console.log(" New issue");
    vr.say("Okay, no problem. Tell me what's going on and I'll help you out.");
    const g = vr.gather({ input: "speech", action: "/new-issue", timeout: 15, hints: "describe,issue,problem,wait,stop" });
    g.say("Go ahead whenever you're ready.");
  }

  vr.redirect("/timeout");
  callState[callSid] = state;
  res.type("text/xml");
  res.send(vr.toString());
});

// ==================== Confirm the ticket for caller from fd====================
app.post("/confirm-ticket", async (req, res) => {
  const callSid = req.body.CallSid;
  const speech = (req.body.SpeechResult || "").toLowerCase().trim();
  const state = callState[callSid];
  state.silenceCount = 0;
  console.log(` User said: "${speech}" (confirm-ticket)`);

  if (speech.includes("wait") || speech.includes("hold") || speech.includes("stop")) {
    return handleUserPause(req, res, "/confirm-ticket");
  }

  const vr = new VoiceResponse();
  const saidYes = speech.includes("yes") || speech.includes("yeah") || speech.includes("correct");

  if (saidYes) {
    const ticket = state.tickets[state.ticketIndex];
    console.log(` Confirmed ticket: "${ticket.subject}"`);
    await provideSolution(req, res, state, ticket);
  } else {
    state.ticketIndex++;
    if (state.tickets[state.ticketIndex]) {
      const next = state.tickets[state.ticketIndex];
      vr.say("Okay, not that one.");
      const g = vr.gather({ input: "speech", action: "/confirm-ticket", timeout: 12 });
      g.say(`How about this: ${next.subject}? Is that it?`);
    } else {
      state.currentStage = "new-issue";
      vr.say("Alright, looks like a new issue.");
      const g = vr.gather({ input: "speech", action: "/new-issue", timeout: 15 });
      g.say("No problem — just tell me what's happening and I'll sort it out.");
    }
    vr.redirect("/timeout");
    callState[callSid] = state;
    res.type("text/xml");
    res.send(vr.toString());
  }
});

// ==================== creating new ticket for new issue ====================
app.post("/new-issue", async (req, res) => {
  const callSid = req.body.CallSid;
  const speech = req.body.SpeechResult || "";
  const state = callState[callSid];
  state.silenceCount = 0;

  if (!speech.trim()) {
    return handleUserPause(req, res, "/new-issue");
  }

  if (speech.toLowerCase().includes("wait") || speech.toLowerCase().includes("hold") || speech.toLowerCase().includes("stop")) {
    return handleUserPause(req, res, "/new-issue");
  }

  state.userDescription = (state.userDescription + " " + speech).trim().slice(-1000);
  console.log(` User described: "${state.userDescription}"`);

  if (!state.newTicketId && state.contact?.id) {
    state.newTicketId = await createTicket(state.contact.id, state.userDescription);
    console.log(` Ticket created: ${state.newTicketId}`);
  }

  await provideSolution(req, res, state);
});

// ==================== PROVIDE SOLUTION (HUMAN & SMART) ====================
async function provideSolution(req, res, state, ticket = null) {
  const callSid = req.body.CallSid;
  const vr = new VoiceResponse();

  let query = ticket?.subject || state.userDescription || "";

  vr.say("One second, let me look this up for you...");
  vr.pause({ length: 2 });

  let kbText = "";
  if (state.kbAttempts < 2) {
    vr.say("Checking our help articles...");
    kbText = await fetchSolutionsSnippet(query);
    state.kbAttempts++;
    state.lastKbText = kbText; // Save for follow-ups
    console.log(`KB search → ${kbText ? "Found content" : "No match"}`);
  }

  let aiAnswer = await callGroqAI({
    mode: ticket ? "existing-ticket" : "new-ticket",
    contact: state.contact,
    ticket,
    question: state.userDescription,
    kb: kbText,
    kbFound: !!kbText.trim(),
  });

  // Freshworks Community fallback
  if (state.kbAttempts >= 2 && !state.communitySearched && !kbText.trim()) {
    state.communitySearched = true;
    vr.say("Hmm, let me also check what others have said in the Freshworks community...");
    vr.pause({ length: 3 });
    const communityInfo = await searchFreshworksCommunity(query);
    aiAnswer = await callGroqAI({
      mode: "community-fallback",
      question: state.userDescription,
      communityInfo,
    });
  }

  // Clarify if common issue (e.g., password)
 

  if (!aiAnswer) {
    vr.say("I'm sorry, I'm not finding clear steps for that right now.");
    vr.say("Let me connect you to a live agent who can help directly.");
    vr.dial(ZOOM_PHONE_DID);
    res.type("text/xml");
    res.send(vr.toString());
    return;
  }

  state.lastAiAnswer = aiAnswer;
  state.currentStage = "after-steps";

  vr.say("Okay, here's what usually works:");
  vr.say(aiAnswer);

  const g = vr.gather({ input: "speech", action: "/after-steps", timeout: 12, hints: "yes,no,worked,still stuck,repeat,agent,wait" });
  g.say("Did that help? Or are you still running into the issue?");
  vr.redirect("/timeout");
  callState[callSid] = state;
  res.type("text/xml");
  res.send(vr.toString());
}

// ==================== AFTER STEPS (SMART FEEDBACK) ====================
app.post("/after-steps", async (req, res) => {
  const callSid = req.body.CallSid;
  const speech = (req.body.SpeechResult || "").toLowerCase().trim();
  const state = callState[callSid];
  state.silenceCount = 0;
  console.log(` Feedback: "${speech}"`);

  if (speech.includes("wait") || speech.includes("hold") || speech.includes("stop")) {
    return handleUserPause(req, res, "/after-steps");
  }

  const vr = new VoiceResponse();

  if (speech.includes("yes") || speech.includes("worked") || speech.includes("fixed") || speech.includes("good")) {
    vr.say("Awesome! I'm really glad that worked.");
    vr.say("Is there anything else I can help you with today?");
    const g = vr.gather({ action: "/anything-else", timeout: 10 });
    g.say("Just say no if we're all set.");
  } else if (speech.includes("repeat") || speech.includes("again")) {
    vr.say("Sure thing, happy to go over it again.");
    vr.say(state.lastAiAnswer || "Let me repeat those steps.");
    vr.redirect("/after-steps");
  } else if (speech.includes("agent") || speech.includes("person")) {
    vr.say("No problem at all — connecting you to a live agent now.");
    vr.dial(ZOOM_PHONE_DID);
  } else {
    // SMART: Treat feedback as follow-up question to Groq
    console.log(" Treating as follow-up: sending to Groq");
    const followUpAnswer = await callGroqAI({
      mode: "follow-up",
      question: speech,
      previousAnswer: state.lastAiAnswer || "",
      kb: state.lastKbText || "",
      kbFound: !!state.lastKbText,
    });

    if (followUpAnswer) {
      state.lastAiAnswer = followUpAnswer;
      vr.say("Got it, let me adjust that for you...");
      vr.say(followUpAnswer);
      const g = vr.gather({ input: "speech", action: "/after-steps", timeout: 12 });
      g.say("Better this time? Or still need something else?");
    } else {
      vr.say("Hmm, let me think on that one.");
      vr.say("Would you like me to connect you to a live agent?");
      const g = vr.gather({ input: "speech", action: "/after-steps", timeout: 8, hints: "yes,agent,no" });
      g.say("Just say yes or no.");
    }
  }

  vr.redirect("/timeout");
  res.type("text/xml");
  res.send(vr.toString());
});

// ==================== ANYTHING ELSE ====================
app.post("/anything-else", (req, res) => {
  const speech = (req.body.SpeechResult || "").toLowerCase();
  const vr = new VoiceResponse();

  if (speech.includes("no") || !speech.trim()) {
    vr.say("Alright, you're all set then. Thanks for calling Sandeza support — have a great day!");
  } else {
    vr.say("Of course, happy to help with that too.");
    const g = vr.gather({ action: "/new-issue", timeout: 15 });
    g.say("Go ahead and tell me about it.");
    vr.redirect("/timeout");
  }
  res.type("text/xml");
  res.send(vr.toString());
});

/* ==================== freshdesk functions ==================== */
async function findContactByPhone(phone) {
  console.log(` Looking up contact by phone: ${phone}`);
  try {
    const url = `https://${FRESHDESK_DOMAIN}.freshdesk.com/api/v2/contacts?phone=${encodeURIComponent(phone)}`;
    const { data } = await axios.get(url, fdAuth());
    const contact = Array.isArray(data) && data.length > 0 ? data[0] : null;
    console.log(contact ? `Found contact ID ${contact.id}` : "No contact found");
    return contact;
  } catch (e) {
    console.log(" findContactByPhone error:", e.message);
    return null;
  }
}

async function createContact(phone) {
  console.log(` Creating new contact for ${phone}`);
  const payload = { name: phone, phone };
  const url = `https://${FRESHDESK_DOMAIN}.freshdesk.com/api/v2/contacts`;
  const { data } = await axios.post(url, payload, fdAuth());
  console.log(`Created contact ID: ${data.id}`);
  return data;
}

async function getRecentTicketsForContact(contactId, limit = 5) {
  console.log(` Fetching recent tickets for contact ${contactId}`);
  const url = `https://${FRESHDESK_DOMAIN}.freshdesk.com/api/v2/tickets`;
  const { data } = await axios.get(url, { params: { requester_id: contactId, order_by: "updated_at", order_type: "desc", per_page: limit }, ...fdAuth() });
  console.log(`Found ${Array.isArray(data) ? data.length : 0} recent tickets`);
  return Array.isArray(data) ? data : [];
}

async function createTicket(contactId, description) {
  console.log(` Creating ticket for contact ${contactId}`);
  const payload = { requester_id: contactId, subject: "Voice support call", description: description || "Voice call issue", status: 2, priority: 1 };
  const url = `https://${FRESHDESK_DOMAIN}.freshdesk.com/api/v2/tickets`;
  const { data } = await axios.post(url, payload, fdAuth());
  console.log(`Ticket created! ID: ${data.id}`);
  return data.id;
}

async function fetchSolutionsSnippet(query) {
  if (!query.trim()) {
    console.log("KB search skipped: empty query");
    return "";
  }

  const words = query
    .toLowerCase()
    .replace(/[^\w\s]/g, '')
    .split(/\s+/)
    .filter(word => word.length > 3)
    .slice(0, 5);

  const searchTerm = words.join(" ") || query.toLowerCase().slice(0, 30);
  console.log(` Original query: "${query}"`);
  console.log(` Searching with: "${searchTerm}"`);

  try {
    const url = `https://sandezainc.freshdesk.com/support/search/solutions.json`;
    const { data } = await axios.get(url, {
      params: { term: searchTerm },
      timeout: 15000,
    });

    const articles = data || [];
    console.log(`Public KB returned ${articles.length} article(s)`);

    if (articles.length === 0) return "";

    const snippets = articles.slice(0, 3).map(article => {
      const title = (article.title || "").replace(/<[^>]*>/g, "").trim();
      const desc = (article.desc || "")
        .replace(/<[^>]*>/g, " ")
        .replace(/\s+/g, " ")
        .trim();
      const fullDesc = (article.source?.article?.desc_un_html || article.source?.article?.description || "")
        .replace(/<[^>]*>/g, " ")
        .replace(/\s+/g, " ")
        .trim();

      return `${title}\n${fullDesc || desc}`;
    }).join("\n\n");

    const limited = snippets.slice(0, 4000);
    console.log(`KB snippet length: ${limited.length} chars`);
    return limited;
  } catch (e) {
    console.log(" KB search error:", e.message);
    return "";
  }
}

async function searchFreshworksCommunity(issue) {
  console.log(` Searching Freshworks Community for: "${issue}"`);
  try {
    const query = encodeURIComponent(`${issue} site:community.freshworks.com`);
    const { data } = await axios.get(`https://www.google.com/search?q=${query}&num=5`, { timeout: 10000 });
    return data.slice(0, 6000);
  } catch (e) {
    console.log("Community search failed:", e.message);
    return "";
  }
}

/* ==================== Groq AI - free modals ==================== */
async function callGroqAI(payload) {
  const { mode, question = "", previousAnswer = "", kb = "", kbFound = false, communityInfo = "" } = payload;

  let systemPrompt = 
    "You are luna, a warm, friendly, and patient human support agent on the phone. " +
    "You speak casually like helping a friend — never read full articles or sound robotic. " +
    "Use short, natural sentences. Add empathy: 'I know that can be frustrating...' " +
    "Only give the steps the customer needs right now. Ask gentle questions if unclear. " +
    "Use fillers sparingly: 'so', 'you know', 'basically'. End with a question like 'Does that help?'";

  let userPrompt = `Customer issue: ${question || "unknown"}\nKB available: ${kbFound}\n`;
  if (kbFound) userPrompt += `Help article content: ${kb.slice(0, 3000)}`;
  if (communityInfo) userPrompt += `\nCommunity discussions: ${communityInfo.slice(0, 3000)}`;

  // Special handling for follow-up
  if (mode === "follow-up") {
    systemPrompt += "\nThis is a follow-up message from the customer after your previous response. Refine or expand based on what they said.";
    userPrompt += `\nPrevious response you gave: ${previousAnswer.slice(0, 2000)}\nCustomer's follow-up: ${question}`;
  }

  userPrompt += "\nTask: Give friendly, conversational guidance — only what's needed.";

  try {
    const completion = await groq.chat.completions.create({
      model: "llama-3.3-70b-versatile",
      messages: [{ role: "system", content: systemPrompt }, { role: "user", content: userPrompt }],
      temperature: 0.6,
      max_tokens: 500,
    });

    let answer = completion.choices[0]?.message?.content?.trim() || "";
    if (!answer) return null;

    answer = answer
      .replace(/Step 1:|First:/gi, "So first,")
      .replace(/Step 2:|Then:/gi, "Then,")
      .replace(/Step 3:|Next:/gi, "After that,")
      .replace(/\.\s+/g, "... ")
      + " ... Does that help, or are you seeing something different?";

    return answer.length > 800 ? answer.slice(0, 780) + "..." : answer;
  } catch (e) {
    console.log("Groq error:", e.message);
    return null;
  }
}

/* ==================== START SERVER ==================== */
app.listen(PORT, () => {
  console.log(`\n  Sandeza Voice Bot is LIVE on port ${PORT}`);
  console.log(`   Use ngrok: ngrok http ${PORT} → set Twilio webhook to that URL + /voice`);
});