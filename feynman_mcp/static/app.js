// Teacher UI: voice (Web Speech API), mouse drawing board, whiteboard camera.
(() => {
  const $ = (id) => document.getElementById(id);
  const MAX_W = 1280;
  let ws, pendingTurn = null, state = null;

  // ---------------------------------------------------------------- websocket
  function connect() {
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws.onopen = () => $("conn").classList.remove("off");
    ws.onclose = () => { $("conn").classList.add("off"); setTimeout(connect, 1500); };
    ws.onmessage = (ev) => handle(JSON.parse(ev.data));
  }
  const send = (obj) => ws && ws.readyState === 1 && ws.send(JSON.stringify(obj));

  function handle(m) {
    switch (m.type) {
      case "session":
        $("log").innerHTML = "";
        $("report-banner").classList.add("hidden");
        renderState(m.state, true);
        break;
      case "state": renderState(m.state, false); break;
      case "question": onQuestion(m.turn_id, m.question, m.wants_diagram); break;
      case "flag": onFlag(m); break;
      case "report":
        $("report-grade").textContent = `${m.score}/100 · ${m.grade}`;
        $("report-link").href = m.url;
        $("report-banner").classList.remove("hidden");
        addMsg("note", "Report generated.");
        speak(`Thanks for teaching me! Your report is ready. Score: ${m.score} out of 100.`);
        break;
      case "snapshot_request": answerSnapshot(m.source); break;
      case "answer_ack": if (!m.ok) addMsg("note", "That question was already answered or replaced."); break;
    }
  }

  function renderState(s, replay) {
    state = s;
    $("topic").textContent = `Teaching: ${s.topic}`;
    $("sub").textContent = `${s.scope === "specific" ? "Specific concept" : "General topic"} · explain it like you're talking to a curious beginner`;
    $("score").textContent = s.assessments.length ? s.score : "–";
    if (replay) {
      for (const t of s.turns) {
        addMsg("ai", t.question);
        if (t.answer) addMsg("me", t.answer + (t.images ? `\n📎 ${t.images} image(s)` : ""));
      }
      for (const a of s.assessments.filter((a) => a.flagged)) addFlagMsg(a);
      if (s.pending_question) setPending(s.pending_question.id, s.pending_question.question, s.pending_question.wants_diagram);
    }
  }

  // ---------------------------------------------------------------- chat log
  function addMsg(kind, text) {
    const d = document.createElement("div");
    d.className = `msg ${kind}`;
    d.textContent = text;
    $("log").appendChild(d);
    $("log").scrollTop = $("log").scrollHeight;
    return d;
  }
  function addFlagMsg(f) {
    const d = addMsg("flag", "");
    d.innerHTML = `<b>⚠️ ${esc(f.severity.toUpperCase())}: ${esc(f.concept)}</b><br>${esc(f.note)}` +
      (f.correction ? `<br><span class="fix">✔ ${esc(f.correction)}</span>` : "");
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  function setPending(id, q, wantsDiagram) {
    pendingTurn = id;
    $("question-bar").textContent = `🧑‍🎓 ${q}`;
    $("question-bar").classList.add("live");
    $("send").disabled = false;
    if (wantsDiagram) {
      $("question-bar").textContent += "  (✏️ draw or use the whiteboard!)";
      if (!camStream) switchTab("draw");
    }
  }

  function onQuestion(id, q, wantsDiagram) {
    addMsg("ai", q);
    setPending(id, q, wantsDiagram);
    speak(q);
  }

  function onFlag(f) {
    addFlagMsg(f);
    $("flag-concept").textContent = f.concept;
    $("flag-note").textContent = f.note;
    $("flag-fix").textContent = f.correction ? `Correct idea: ${f.correction}` : "";
    $("flag-modal").classList.remove("hidden");
    speak(`Hold on, important knowledge check about ${f.concept}. ${f.note}. ${f.correction ? "The correct idea: " + f.correction : ""}`);
  }
  $("flag-ok").onclick = () => $("flag-modal").classList.add("hidden");

  // ---------------------------------------------------------------- text to speech
  function speak(text) {
    if (!$("tts").checked || !("speechSynthesis" in window)) return;
    stopMic();
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 1.03;
    speechSynthesis.speak(u);
  }

  // ---------------------------------------------------------------- speech to text
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let rec = null, listening = false, baseText = "";
  if (!SR) {
    $("mic-note").textContent = "Voice input needs Chrome or Edge. You can still type your answer.";
    $("mic").disabled = true;
  }
  function startMic() {
    if (!SR || listening) return;
    speechSynthesis?.cancel();
    rec = new SR();
    rec.continuous = true;
    rec.interimResults = true;
    rec.lang = navigator.language || "en-US";
    baseText = $("answer").value ? $("answer").value.trimEnd() + " " : "";
    rec.onresult = (e) => {
      let finalT = "", interim = "";
      for (let i = 0; i < e.results.length; i++) {
        const r = e.results[i];
        (r.isFinal ? (finalT += r[0].transcript) : (interim += r[0].transcript));
      }
      $("answer").value = baseText + finalT + interim;
    };
    rec.onerror = (e) => { $("mic-note").textContent = `Mic: ${e.error}`; };
    rec.onend = () => { if (listening) { baseText = $("answer").value + " "; try { rec.start(); } catch {} } };
    rec.start();
    listening = true;
    $("mic").classList.add("on");
    $("mic-note").textContent = "Listening… click the mic again (or press Space outside the text box) to stop.";
  }
  function stopMic() {
    if (!listening) return;
    listening = false;
    try { rec.stop(); } catch {}
    $("mic").classList.remove("on");
    $("mic-note").textContent = "";
  }
  $("mic").onclick = () => (listening ? stopMic() : startMic());

  // ---------------------------------------------------------------- drawing board
  const cv = $("canvas"), cx = cv.getContext("2d");
  let strokes = [], cur = null, color = "#1d1d1f", erasing = false, labeling = false;
  function pos(e) {
    const r = cv.getBoundingClientRect();
    return [((e.clientX - r.left) / r.width) * cv.width, ((e.clientY - r.top) / r.height) * cv.height];
  }
  function redraw() {
    cx.fillStyle = "#fff";
    cx.fillRect(0, 0, cv.width, cv.height);
    for (const s of strokes) drawStroke(s);
  }
  function drawStroke(s) {
    if (s.text) {
      cx.fillStyle = s.color;
      cx.font = `${s.size * 6 + 14}px system-ui, sans-serif`;
      cx.fillText(s.text, s.pts[0][0], s.pts[0][1]);
      return;
    }
    cx.strokeStyle = s.erase ? "#fff" : s.color;
    cx.lineWidth = s.erase ? s.size * 5 : s.size;
    cx.lineCap = cx.lineJoin = "round";
    cx.beginPath();
    s.pts.forEach(([x, y], i) => (i ? cx.lineTo(x, y) : cx.moveTo(x, y)));
    if (s.pts.length === 1) cx.lineTo(s.pts[0][0] + 0.1, s.pts[0][1]);
    cx.stroke();
  }
  cv.addEventListener("pointerdown", (e) => {
    const p = pos(e);
    if (labeling) {
      const text = prompt("Label text:");
      if (text) { strokes.push({ text, pts: [p], color, size: +$("size").value }); redraw(); markChanged(); }
      return;
    }
    cv.setPointerCapture(e.pointerId);
    cur = { pts: [p], color, size: +$("size").value, erase: erasing };
    strokes.push(cur);
    drawStroke(cur);
  });
  cv.addEventListener("pointermove", (e) => {
    if (!cur) return;
    cur.pts.push(pos(e));
    redraw();
  });
  const endStroke = () => { if (cur) { cur = null; markChanged(); } };
  cv.addEventListener("pointerup", endStroke);
  cv.addEventListener("pointercancel", endStroke);
  function markChanged() {
    $("att-draw").checked = strokes.length > 0;
  }
  function setTool(btn) {
    for (const b of document.querySelectorAll(".sw, #eraser, #text-tool")) b.classList.remove("active");
    btn.classList.add("active");
  }
  document.querySelectorAll(".sw").forEach((b) => (b.onclick = () => { color = b.dataset.color; erasing = labeling = false; setTool(b); }));
  $("eraser").onclick = () => { erasing = true; labeling = false; setTool($("eraser")); };
  $("text-tool").onclick = () => { labeling = true; erasing = false; setTool($("text-tool")); };
  $("undo").onclick = () => { strokes.pop(); redraw(); markChanged(); };
  $("clear").onclick = () => { strokes = []; redraw(); markChanged(); };
  redraw();

  function canvasImage() {
    if (!strokes.length) return null;
    return { source: "canvas", dataUrl: scaled(cv, cv.width, cv.height, "image/png") };
  }

  // ---------------------------------------------------------------- camera
  let camStream = null, lastSnap = null;
  const video = $("video");
  async function startCam(deviceId) {
    try {
      camStream?.getTracks().forEach((t) => t.stop());
      camStream = await navigator.mediaDevices.getUserMedia({
        video: deviceId ? { deviceId: { exact: deviceId } } : { facingMode: "environment", width: { ideal: 1920 } },
      });
      video.srcObject = camStream;
      $("cam-snap").disabled = false;
      $("att-cam").checked = true;
      $("cam-start").textContent = "■ Stop camera";
      const cams = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
      if (cams.length > 1) {
        $("cam-select").innerHTML = cams.map((c, i) => `<option value="${c.deviceId}">${esc(c.label || "Camera " + (i + 1))}</option>`).join("");
        $("cam-select").value = camStream.getVideoTracks()[0].getSettings().deviceId || "";
        $("cam-select").classList.remove("hidden");
      }
    } catch (err) {
      addMsg("note", `Camera unavailable: ${err.message}`);
    }
  }
  function stopCam() {
    camStream?.getTracks().forEach((t) => t.stop());
    camStream = null;
    video.srcObject = null;
    $("cam-snap").disabled = true;
    $("att-cam").checked = !!lastSnap;
    $("cam-start").textContent = "▶ Start camera";
  }
  $("cam-start").onclick = () => (camStream ? stopCam() : startCam());
  $("cam-select").onchange = (e) => startCam(e.target.value);
  function grabFrame() {
    if (!camStream || !video.videoWidth) return lastSnap;
    lastSnap = scaled(video, video.videoWidth, video.videoHeight, "image/jpeg");
    $("cam-preview").src = lastSnap;
    $("cam-preview").classList.remove("hidden");
    return lastSnap;
  }
  $("cam-snap").onclick = () => { grabFrame(); $("att-cam").checked = true; };
  function cameraImage() {
    const url = grabFrame();
    return url ? { source: "camera", dataUrl: url } : null;
  }

  function scaled(src, w, h, type) {
    const k = Math.min(1, MAX_W / w);
    const c = document.createElement("canvas");
    c.width = Math.round(w * k);
    c.height = Math.round(h * k);
    const g = c.getContext("2d");
    g.fillStyle = "#fff";
    g.fillRect(0, 0, c.width, c.height);
    g.drawImage(src, 0, 0, c.width, c.height);
    return c.toDataURL(type, 0.85);
  }

  // ---------------------------------------------------------------- tabs
  function switchTab(name) {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    $("tab-draw").classList.toggle("hidden", name !== "draw");
    $("tab-cam").classList.toggle("hidden", name !== "cam");
  }
  document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => switchTab(t.dataset.tab)));

  // ---------------------------------------------------------------- sending
  function submit() {
    if (!pendingTurn) return;
    stopMic();
    const images = [];
    if ($("att-draw").checked) { const i = canvasImage(); if (i) images.push(i); }
    if ($("att-cam").checked) { const i = cameraImage(); if (i) images.push(i); }
    const text = $("answer").value.trim();
    if (!text && !images.length) { $("mic-note").textContent = "Say or type something (or attach a diagram) first."; return; }
    send({ type: "answer", turn_id: pendingTurn, text, images });
    addMsg("me", (text || "(diagram only)") + (images.length ? `\n📎 ${images.map((i) => i.source).join(", ")}` : ""));
    pendingTurn = null;
    $("answer").value = "";
    $("att-draw").checked = false;
    $("att-cam").checked = !!camStream;
    $("send").disabled = true;
    $("question-bar").textContent = "Your student is thinking…";
    $("question-bar").classList.remove("live");
  }
  $("send").onclick = submit;
  $("answer").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } });
  document.addEventListener("keydown", (e) => {
    const typing = ["TEXTAREA", "INPUT", "SELECT"].includes(document.activeElement.tagName);
    if (e.code === "Space" && !typing) { e.preventDefault(); listening ? stopMic() : startMic(); }
    if ((e.ctrlKey || e.metaKey) && e.key === "z" && !typing) { e.preventDefault(); $("undo").click(); }
  });

  function answerSnapshot(source) {
    const images = [];
    if (source !== "camera") { const i = canvasImage(); if (i) images.push(i); }
    if (source !== "canvas") { const i = cameraImage(); if (i) images.push(i); }
    send({ type: "snapshot", images });
  }

  connect();
})();
