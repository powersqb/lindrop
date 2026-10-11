"use strict";
const $ = id => document.getElementById(id);
let active = null, busy = false, timer = null;
function message(text, ok = false) { $("message").textContent = text; $("message").className = ok ? "success" : ""; }
function connected(value) {
  $("pair-panel").hidden = value; $("transfer-panel").hidden = !value;
  clearInterval(timer); timer = value ? setInterval(() => { if (!busy) refresh(false); }, 5000) : null;
}
async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  const data = await response.json();
  if (!response.ok) { if (response.status === 401) connected(false); throw Error(data.error || "请求失败"); }
  return data;
}
function size(bytes) { if (bytes < 1024) return `${bytes} B`; const units = ["KiB", "MiB", "GiB"]; let i = -1; do {bytes /= 1024; i++;} while (bytes >= 1024 && i < 2); return `${bytes.toFixed(1)} ${units[i]}`; }
async function refresh(showError = true) {
  try {
    const data = await api("/api/files"); connectedIfNeeded(); $("count").textContent = data.files.length;
    $("file-list").replaceChildren();
    if (!data.files.length) { const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "还没有文件，传一个试试。"; $("file-list").append(empty); }
    for (const file of data.files.reverse()) {
      const row = document.createElement("div"); row.className = "file-row";
      const icon = document.createElement("span"); icon.className = "file-icon"; icon.textContent = "↧";
      const detail = document.createElement("div"); detail.className = "file-detail";
      const name = document.createElement("div"); name.className = "file-name"; name.textContent = file.name; name.title = file.name;
      const meta = document.createElement("div"); meta.className = "file-meta"; meta.textContent = `${size(file.size)} · SHA-256 ${file.sha256.slice(0,12)}…`; meta.title = `完整校验值：${file.sha256}`;
      const link = document.createElement("a"); link.className = "download"; link.textContent = "下载 ↓"; link.href = `/download/${encodeURIComponent(file.id)}`; link.download = file.name;
      detail.append(name,meta); row.append(icon,detail,link); $("file-list").append(row);
    }
  } catch (error) { if (showError) message(error.message); }
}
function connectedIfNeeded() { if ($("transfer-panel").hidden) connected(true); }
$("pair-form").addEventListener("submit", async event => {
  event.preventDefault(); $("pair-button").disabled = true;
  try { await api("/api/pair", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({code:$("code").value})}); $("code").value = ""; connected(true); message("连接成功，可以开始传输。",true); await refresh(); }
  catch (error) { message(error.message); } finally { $("pair-button").disabled = false; }
});
$("refresh").onclick = () => refresh();
$("logout").onclick = async () => { try { if (active) active.abort(); await api("/api/logout",{method:"POST"}); connected(false); message("已断开连接。",true); } catch(error) {message(error.message);} };
$("choose").onclick = () => $("files").click();
$("files").onchange = () => { sendFiles(Array.from($("files").files)); $("files").value = ""; };
$("cancel").onclick = () => { if (active) active.abort(); };
for (const type of ["dragenter","dragover"]) $("drop-zone").addEventListener(type,event => {event.preventDefault(); $("drop-zone").classList.add("dragging");});
$("drop-zone").addEventListener("dragleave",() => $("drop-zone").classList.remove("dragging"));
$("drop-zone").addEventListener("drop",event => {event.preventDefault(); $("drop-zone").classList.remove("dragging"); sendFiles(Array.from(event.dataTransfer.files));});
function upload(file,index,total) {
  return new Promise((resolve,reject) => {
    const xhr = new XMLHttpRequest(); active = xhr; xhr.open("POST","/api/upload"); xhr.timeout = 3600000; xhr.setRequestHeader("X-Filename",encodeURIComponent(file.name));
    $("progress-name").textContent = `${index}/${total} · ${file.name}`; $("progress").value = 0; $("progress-text").textContent = "准备传输…";
    const start = performance.now();
    xhr.upload.onprogress = event => { if(event.lengthComputable) { const percent = event.loaded / event.total * 100; $("progress").value = percent; const speed = event.loaded / Math.max((performance.now()-start)/1000,0.1); $("progress-text").textContent = `${Math.round(percent)}% · ${size(event.loaded)} / ${size(event.total)} · ${size(speed)}/s${percent === 100 ? " · 正在确认保存" : ""}`; } };
    xhr.onload = () => { let data; try {data = JSON.parse(xhr.responseText);} catch {reject(Error("服务响应异常"));return;} if(xhr.status === 201) resolve(data); else {if(xhr.status === 401) connected(false); reject(Error(data.error || "传输失败"));} };
    xhr.onerror = () => reject(Error("连接中断，请检查 Wi-Fi 和电脑上的服务。")); xhr.ontimeout = () => reject(Error("传输超时，请重试。")); xhr.onabort = () => reject(Error("已取消传输，后续文件未发送。")); xhr.send(file);
  });
}
async function sendFiles(files) {
  if(busy || !files.length) return;
  busy = true; $("choose").disabled = true; $("progress-panel").hidden = false; message(""); let completed = 0;
  try { for(const file of files) { if(file.size > 2*1024**3) throw Error(`${file.name} 超过 2 GiB 上限。`); await upload(file,completed+1,files.length); completed++; await refresh(false); } message(`已传输 ${completed} 个文件，保存在电脑上。`,true); }
  catch(error) { message(`${error.message}${completed ? ` 已完成 ${completed} 个文件。` : ""}`); }
  finally {active = null; busy = false; $("choose").disabled = false; $("progress-panel").hidden = true;}
}
refresh(false);
