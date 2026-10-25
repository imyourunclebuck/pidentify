"""Single-page camctl controls. No external assets.

The page talks to the same origin on port 8555, so a Tailscale MagicDNS name
works as http://<name>:8555/ui. Controls are built from the capabilities
object. When control_token is set, the browser's HTTP Basic prompt covers
this page and the preview image.
"""

from __future__ import annotations

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pidentify camera control</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font: 15px/1.4 ui-sans-serif, system-ui, sans-serif; background: #121416; color: #e7e4dc; }
  header, main { padding: 16px 20px; }
  header { border-bottom: 1px solid #2c3136; display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
  h1 { font-size: 18px; margin: 0; font-weight: 600; }
  select, button, input { background: #1c2126; color: inherit; border: 1px solid #3a4148; border-radius: 6px; padding: 6px 8px; }
  button { cursor: pointer; }
  button:disabled { opacity: 0.45; cursor: default; }
  .layout { display: grid; grid-template-columns: minmax(280px, 420px) 1fr; gap: 20px; }
  @media (max-width: 800px) { .layout { grid-template-columns: 1fr; } }
  img.preview { width: 100%; background: #000; aspect-ratio: 16/9; object-fit: contain; border-radius: 8px; }
  .note { color: #b7b1a4; font-size: 13px; }
  fieldset { border: 1px solid #2c3136; border-radius: 8px; margin: 0 0 12px; }
  legend { padding: 0 6px; }
  label { display: grid; grid-template-columns: 9.5rem 1fr; gap: 8px; align-items: center; margin: 6px 0; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
  #status { min-height: 1.4em; }
  .err { color: #ffb4a8; }
</style>
</head>
<body>
<header>
  <h1>pidentify</h1>
  <label>Camera <select id="camera"></select></label>
  <span id="model" class="note"></span>
</header>
<main>
  <div class="layout">
    <section>
      <img id="preview" class="preview" alt="live preview">
      <p class="note" id="preview-note"></p>
      <div class="row">
        <button id="autofocus" type="button">Autofocus</button>
        <button id="snapshot" type="button">Snapshot</button>
        <label>Record seconds <input id="seconds" type="number" min="1" max="120" value="10"></label>
        <button id="record" type="button">Record</button>
      </div>
      <p id="status"></p>
      <ul id="clips"></ul>
    </section>
    <form id="controls">
      <fieldset><legend>Stream</legend><div id="stream"></div></fieldset>
      <fieldset><legend>Image</legend><div id="image"></div></fieldset>
      <fieldset><legend>Autofocus</legend><div id="focus"></div></fieldset>
      <div class="row">
        <button type="submit">Apply</button>
        <button id="reset" type="button">Reset to defaults</button>
      </div>
    </form>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);
let caps = null;
let settings = null;
let cam = null;

function say(text, bad) {
  $("status").textContent = text || "";
  $("status").className = bad ? "err" : "";
}

async function api(path, options) {
  const response = await fetch(path, options);
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    const body = await response.json();
    if (!response.ok) {
      const fields = body.fields ? " " + Object.values(body.fields).join("; ") : "";
      throw new Error((body.error || response.statusText) + fields);
    }
    return {response, body};
  }
  if (!response.ok) throw new Error(response.statusText);
  return {response, blob: await response.blob()};
}

function field(parent, title, control) {
  const label = document.createElement("label");
  label.appendChild(document.createTextNode(title));
  label.appendChild(control);
  parent.appendChild(label);
  return control;
}

function select(options, value) {
  const node = document.createElement("select");
  options.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.value;
    option.textContent = item.label;
    if (String(item.value) === String(value)) option.selected = true;
    node.appendChild(option);
  });
  return node;
}

function slider(min, max, step, value) {
  const node = document.createElement("input");
  node.type = "range";
  node.min = min;
  node.max = max;
  node.step = step;
  node.value = value == null ? min : value;
  const out = document.createElement("span");
  const wrap = document.createElement("span");
  out.textContent = node.value;
  node.addEventListener("input", () => { out.textContent = node.value; });
  wrap.appendChild(node);
  wrap.appendChild(out);
  wrap.valueOf = () => node;
  wrap.input = node;
  return wrap;
}

function readNumber(node) {
  const raw = node.input ? node.input.value : node.value;
  if (raw === "" || raw == null) return null;
  const number = Number(raw);
  return Number.isFinite(number) ? number : null;
}

function build() {
  const stream = $("stream");
  const image = $("image");
  const focus = $("focus");
  stream.replaceChildren();
  image.replaceChildren();
  focus.replaceChildren();
  const resolutions = (caps.resolutions || []).map((item) => {
    const label = item.width + "x" + item.height;
    return {value: label, label: label + " (up to " + item.max_fps + " fps)"};
  });
  if (!resolutions.length) {
    resolutions.push({value: settings.width + "x" + settings.height, label: "current"});
  }
  const currentSize = settings.width + "x" + settings.height;
  field(stream, "Resolution", select(resolutions, currentSize)).id = "f-size";
  field(stream, "Quality", select([{value: "", label: "custom"}].concat(caps.quality.map((name) => ({value: name, label: name}))), settings.quality || "")).id = "f-quality";
  field(stream, "Framerate", Object.assign(slider(caps.fps.min, caps.fps.max, 1, settings.fps), {})).id = "f-fps";
  field(stream, "Bitrate", slider(caps.bitrate.min, caps.bitrate.max, 100000, settings.bitrate)).id = "f-bitrate";
  field(stream, "Keyframe", slider(caps.keyframe_interval.min, caps.keyframe_interval.max, 1, settings.keyframe_interval || settings.fps)).id = "f-gop";
  field(stream, "x264 preset", select(caps.x264_preset.map((name) => ({value: name, label: name})), settings.x264_preset)).id = "f-preset";
  field(stream, "x264 tune", select(caps.x264_tune.map((name) => ({value: name, label: name})), settings.x264_tune)).id = "f-tune";
  const hdrValues = caps.hdr.supported ? caps.hdr.values : ["off"];
  field(stream, "HDR", select(hdrValues.map((name) => ({value: name, label: name})), settings.hdr || "off")).id = "f-hdr";
  field(stream, "Denoise", select([{value: "", label: "default"}].concat(caps.denoise.map((name) => ({value: name, label: name}))), settings.denoise || "")).id = "f-denoise";
  field(stream, "Rotation", select([{value: "0", label: "0"}, {value: "180", label: "180"}], settings.rotation)).id = "f-rotation";
  const hflip = document.createElement("input");
  hflip.type = "checkbox";
  hflip.checked = !!settings.hflip;
  hflip.id = "f-hflip";
  field(stream, "H flip", hflip);
  const vflip = document.createElement("input");
  vflip.type = "checkbox";
  vflip.checked = !!settings.vflip;
  vflip.id = "f-vflip";
  field(stream, "V flip", vflip);

  field(image, "Exposure", select([{value: "", label: "default"}].concat(caps.exposure.map((name) => ({value: name, label: name}))), settings.exposure || "")).id = "f-exposure";
  field(image, "Shutter us", slider(0, 100000, 100, settings.shutter || 0)).id = "f-shutter";
  field(image, "Gain", slider(0, caps.gain.max, 0.1, settings.gain || 0)).id = "f-gain";
  field(image, "EV", slider(caps.ev.min, caps.ev.max, 0.1, settings.ev || 0)).id = "f-ev";
  field(image, "Metering", select([{value: "", label: "default"}].concat(caps.metering.map((name) => ({value: name, label: name}))), settings.metering || "")).id = "f-metering";
  field(image, "AWB", select([{value: "", label: "default"}].concat(caps.awb.map((name) => ({value: name, label: name}))), settings.awb || "")).id = "f-awb";
  field(image, "AWB red", slider(0, 8, 0.01, (settings.awbgains && settings.awbgains[0]) || 1)).id = "f-red";
  field(image, "AWB blue", slider(0, 8, 0.01, (settings.awbgains && settings.awbgains[1]) || 1)).id = "f-blue";
  field(image, "Brightness", slider(caps.brightness.min, caps.brightness.max, 0.01, settings.brightness || 0)).id = "f-brightness";
  field(image, "Contrast", slider(caps.contrast.min, Math.min(caps.contrast.max, 4), 0.01, settings.contrast == null ? 1 : settings.contrast)).id = "f-contrast";
  field(image, "Saturation", slider(caps.saturation.min, Math.min(caps.saturation.max, 4), 0.01, settings.saturation == null ? 1 : settings.saturation)).id = "f-saturation";
  field(image, "Sharpness", slider(caps.sharpness.min, Math.min(caps.sharpness.max, 4), 0.01, settings.sharpness == null ? 1 : settings.sharpness)).id = "f-sharpness";

  const af = caps.autofocus || {};
  $("autofocus").disabled = !af.supported;
  if (!af.supported) {
    const p = document.createElement("p");
    p.className = "note";
    p.textContent = af.note || "autofocus is not supported";
    focus.appendChild(p);
    return;
  }
  field(focus, "Mode", select([{value: "", label: "default"}].concat(af.mode.map((name) => ({value: name, label: name}))), settings.autofocus_mode || "")).id = "f-afmode";
  field(focus, "Range", select([{value: "", label: "default"}].concat(af.range.map((name) => ({value: name, label: name}))), settings.af_range || "")).id = "f-afrange";
  field(focus, "Speed", select([{value: "", label: "default"}].concat(af.speed.map((name) => ({value: name, label: name}))), settings.af_speed || "")).id = "f-afspeed";
  const lens = af.lens_position || {min: 0, max: 10};
  field(focus, "Lens", slider(lens.min, lens.max, 0.05, settings.lens_position || 0)).id = "f-lens";
  const windowValue = settings.af_window || {x: 0, y: 0, w: 1, h: 1};
  field(focus, "Window x", slider(0, 1, 0.01, windowValue.x)).id = "f-wx";
  field(focus, "Window y", slider(0, 1, 0.01, windowValue.y)).id = "f-wy";
  field(focus, "Window w", slider(0.05, 1, 0.01, windowValue.w)).id = "f-ww";
  field(focus, "Window h", slider(0.05, 1, 0.01, windowValue.h)).id = "f-wh";
}

function emptyToNull(value) {
  return value === "" ? null : value;
}

function collect() {
  const size = $("f-size").value.split("x");
  const quality = emptyToNull($("f-quality").value);
  const body = {
    width: Number(size[0]),
    height: Number(size[1]),
    fps: readNumber($("f-fps")),
    bitrate: readNumber($("f-bitrate")),
    keyframe_interval: readNumber($("f-gop")),
    x264_preset: $("f-preset").value,
    x264_tune: $("f-tune").value,
    hdr: $("f-hdr").value === "off" ? "off" : $("f-hdr").value,
    denoise: emptyToNull($("f-denoise").value),
    rotation: Number($("f-rotation").value),
    hflip: $("f-hflip").checked,
    vflip: $("f-vflip").checked,
    exposure: emptyToNull($("f-exposure").value),
    shutter: readNumber($("f-shutter")),
    gain: readNumber($("f-gain")),
    ev: readNumber($("f-ev")),
    metering: emptyToNull($("f-metering").value),
    awb: emptyToNull($("f-awb").value),
    brightness: readNumber($("f-brightness")),
    contrast: readNumber($("f-contrast")),
    saturation: readNumber($("f-saturation")),
    sharpness: readNumber($("f-sharpness"))
  };
  if (quality) {
    body.quality = quality;
    delete body.width;
    delete body.height;
    delete body.fps;
    delete body.bitrate;
    delete body.keyframe_interval;
    delete body.x264_preset;
    delete body.x264_tune;
  }
  if (body.awb === "custom") {
    body.awbgains = [readNumber($("f-red")), readNumber($("f-blue"))];
  } else {
    body.awbgains = null;
  }
  if (caps.autofocus && caps.autofocus.supported) {
    body.autofocus_mode = emptyToNull($("f-afmode").value);
    body.af_range = emptyToNull($("f-afrange").value);
    body.af_speed = emptyToNull($("f-afspeed").value);
    body.lens_position = readNumber($("f-lens"));
    body.af_window = {
      x: readNumber($("f-wx")),
      y: readNumber($("f-wy")),
      w: readNumber($("f-ww")),
      h: readNumber($("f-wh"))
    };
  }
  return body;
}

async function loadCamera() {
  cam = $("camera").value;
  const {body} = await api("/cameras/" + cam + "/settings");
  settings = body.settings;
  caps = body.capabilities;
  $("model").textContent = (caps.model || "") + (caps.noir ? " NoIR" : "") + (caps.encoder ? " / " + caps.encoder : "");
  $("preview-note").textContent = caps.snapshot_note || "";
  build();
  refreshPreview();
  loadClips();
}

function refreshPreview() {
  if (!cam) return;
  $("preview").src = "/cameras/" + cam + "/preview.jpg?t=" + Date.now();
}

async function loadClips() {
  const {body} = await api("/cameras/" + cam + "/captures");
  const list = $("clips");
  list.replaceChildren();
  (body.captures || []).forEach((item) => {
    const li = document.createElement("li");
    const link = document.createElement("a");
    link.href = item.download;
    link.textContent = item.name + " (" + item.bytes + " bytes)";
    li.appendChild(link);
    list.appendChild(li);
  });
}

async function loadCameras() {
  const {body} = await api("/cameras");
  const picker = $("camera");
  picker.replaceChildren();
  (body.cameras || []).forEach((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.id + (item.model ? " " + item.model : "");
    picker.appendChild(option);
  });
  if (!picker.options.length) {
    say("No cameras in cameras.json", true);
    return;
  }
  await loadCamera();
}

$("camera").addEventListener("change", () => loadCamera().catch((error) => say(error.message, true)));
$("controls").addEventListener("submit", (event) => {
  event.preventDefault();
  api("/cameras/" + cam + "/settings", {
    method: "PUT",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(collect())
  }).then(({body}) => {
    settings = body.settings;
    const restart = body.restart && body.restart.detail ? body.restart.detail : "saved";
    say("Saved. " + restart, body.restart && body.restart.ok === false);
    return loadCamera();
  }).catch((error) => say(error.message, true));
});
$("reset").addEventListener("click", () => {
  api("/cameras/" + cam + "/settings", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({reset: true})
  }).then(() => loadCamera()).then(() => say("Reset to defaults")).catch((error) => say(error.message, true));
});
$("autofocus").addEventListener("click", () => {
  say("Running autofocus…");
  api("/cameras/" + cam + "/autofocus", {method: "POST"}).then(({body}) => {
    say("Lens position " + body.lens_position + " (" + (body.af_state || "done") + "). Stream gap " + body.interrupted_ms + " ms.");
  }).catch((error) => say(error.message, true));
});
$("snapshot").addEventListener("click", () => {
  say("Taking snapshot…");
  api("/cameras/" + cam + "/snapshot", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({})
  }).then(({response, blob}) => {
    const gap = response.headers.get("X-Stream-Interrupted-Ms");
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = cam + ".jpg";
    link.click();
    say("Snapshot saved. Stream interrupted for " + (gap || "?") + " ms.");
  }).catch((error) => say(error.message, true));
});
$("record").addEventListener("click", () => {
  const seconds = Number($("seconds").value);
  say("Recording " + seconds + "s from the live stream…");
  api("/cameras/" + cam + "/record", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({seconds: seconds})
  }).then(({body}) => {
    say((body.note || "Recorded") + " " + body.file);
    loadClips();
  }).catch((error) => say(error.message, true));
});
setInterval(refreshPreview, 1000);
loadCameras().catch((error) => say(error.message, true));
</script>
</body>
</html>
"""


def render_ui() -> str:
    return PAGE
