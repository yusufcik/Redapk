"""
REDgram backend — Telethon (haqiqiy Telegram ulanishi) + aiohttp.

BITTA FAYL: backend va sayt (index.html) shu faylning ichida.
Kutubxonalar (aiohttp, telethon) yo'q bo'lsa, o'zi o'rnatadi.

Ishga tushirish:
    API_ID=12345 API_HASH=abcdef... python redgram.py
So'ng brauzerda http://localhost:8080 ni oching.

Muhit o'zgaruvchilari:
    API_ID, API_HASH   — my.telegram.org dan (majburiy)
    PORT               — server porti (Railway o'zi beradi, standart 8080)
    DATA_DIR           — sessiyalar saqlanadigan papka (standart: joriy papka)
    ALLOWED_ORIGIN     — frontend boshqa joyda (masalan GitHub Pages) bo'lsa, shu manzil
"""
import asyncio
import io
import json
import logging
import os
import secrets
import subprocess
import sys
import time
from collections import OrderedDict
from urllib.parse import quote

try:
    import aiohttp  # noqa: F401
    import telethon  # noqa: F401
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "aiohttp", "telethon"])

from aiohttp import web
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("redgram")

try:
    API_ID = int(os.environ["API_ID"])
    API_HASH = os.environ["API_HASH"]
except (KeyError, ValueError):
    raise SystemExit("API_ID va API_HASH muhit o'zgaruvchilarini kiriting (my.telegram.org)")

PORT = int(os.environ.get("PORT", "8080"))
DATA_DIR = os.environ.get("DATA_DIR", ".")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")

os.makedirs(DATA_DIR, exist_ok=True)

clients = {}        # token -> kirgan TelegramClient
pending = {}        # token -> kod kutilayotgan sessiya
avatar_cache = {}   # (token, chat_id) -> bytes
last_read = {}      # (token, chat_id) -> oxirgi o'qilgan xabar id
avatar_sem = asyncio.Semaphore(3)
media_sem = asyncio.Semaphore(3)
sockets = {}        # token -> {WebSocketResponse}
media_cache = OrderedDict()   # (token, chat_id, msg_id, thumb) -> (bytes, ctype, name)
media_bytes = 0
MAX_MEDIA = 30 * 1024 * 1024
MAX_CACHE = 150 * 1024 * 1024

ERROR_TEXTS = {
    "PhoneNumberInvalidError": "Telefon raqam noto'g'ri",
    "PhoneNumberBannedError": "Bu raqam Telegramda bloklangan",
    "PhoneNumberFloodError": "Juda ko'p urinish. Keyinroq qayta urining",
    "PhoneCodeInvalidError": "Kod noto'g'ri",
    "PhoneCodeExpiredError": "Kod eskirgan, qaytadan kod oling",
    "PasswordHashInvalidError": "2FA parol noto'g'ri",
    "ApiIdInvalidError": "API_ID yoki API_HASH noto'g'ri",
    "MessageTooLongError": "Xabar juda uzun",
    "MessageNotModifiedError": "Matn o'zgarmagan",
    "MessageAuthorRequiredError": "Faqat o'z xabaringizni tahrirlash mumkin",
    "MessageEditTimeExpiredError": "Tahrirlash vaqti o'tib ketgan",
    "MessageIdInvalidError": "Xabar topilmadi",
    "ChatAdminRequiredError": "Buning uchun ruxsat yo'q",
    "ChatWriteForbiddenError": "Bu chatga yozish mumkin emas",
}


# ---------------------------------------------------------------- sayt (index.html)
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="uz" class="h-full select-none">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>REDgram — Yangi Avlod Qizil Telegram Klienti</title>
  <script src="https://cdn.tailwindcss.com/3.4.17"></script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap');
    :root { --b1: #dc2626; --b2: #b91c1c; --fs: 12px; }
    * { box-sizing: border-box; font-family: 'Plus Jakarta Sans', sans-serif; }
    ::-webkit-scrollbar { width: 5px; height: 5px; }
    ::-webkit-scrollbar-thumb { background: rgba(239, 68, 68, 0.3); border-radius: 9999px; }
    .red-glow { box-shadow: 0 0 25px -3px rgba(220, 38, 38, 0.45); }
    .bubble-sent { background: linear-gradient(135deg, var(--b1) 0%, var(--b2) 100%); border-top-right-radius: 4px; }
    .bubble-recv { background: #1e293b; border-top-left-radius: 4px; }
    #messages-container { font-size: var(--fs); }
  </style>
</head>
<body class="h-full w-full bg-[#0a0d14] text-slate-100 overflow-hidden flex flex-col">

  <!-- AUTHENTICATION SCREEN -->
  <div id="auth-screen" class="absolute inset-0 z-40 bg-[#07090e] flex items-center justify-center p-4">
    <div class="relative max-w-md w-full bg-[#121622]/95 border border-red-500/25 rounded-3xl p-6 sm:p-8 shadow-2xl backdrop-blur-xl">

      <div class="text-center mb-7">
        <div class="inline-block relative mb-3">
          <svg class="w-20 h-20 mx-auto red-glow rounded-3xl" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
            <rect width="100" height="100" rx="24" fill="#161b26" stroke="#ef4444" stroke-width="2.5" />
            <path d="M22 48L82 24L56 82L44 56L22 48Z" fill="url(#redGlow)" />
            <path d="M44 56L82 24L56 82L44 56Z" fill="#b91c1c" fill-opacity="0.6"/>
            <path d="M44 56V72L52 64L44 56Z" fill="#ef4444"/>
            <defs>
              <linearGradient id="redGlow" x1="22" y1="24" x2="82" y2="82" gradientUnits="userSpaceOnUse">
                <stop stop-color="#f87171"/>
                <stop offset="1" stop-color="#b91c1c"/>
              </linearGradient>
            </defs>
          </svg>
          <span class="absolute -bottom-1 -right-1 bg-red-600 text-[10px] font-black uppercase px-2 py-0.5 rounded-full border border-slate-900 tracking-wider">v1.1</span>
        </div>
        <h1 class="text-2xl sm:text-3xl font-extrabold tracking-tight text-white flex items-center justify-center space-x-1">
          <span>RED</span><span class="text-red-500">gram</span>
        </h1>
        <p class="text-xs text-slate-400 mt-1">Yangi avlod tezkor va xavfsiz Telegram klienti</p>
      </div>

      <div id="auth-error" class="hidden mb-4 text-xs text-center text-red-300 bg-red-950/50 border border-red-500/30 rounded-xl px-3 py-2"></div>

      <div id="step-phone" class="space-y-5">
        <div>
          <label class="block text-xs font-semibold text-slate-300 mb-1.5">Telefon raqamingiz</label>
          <div class="flex rounded-xl bg-slate-900/90 border border-slate-700 focus-within:border-red-500 transition px-3 py-3 items-center">
            <span class="text-sm mr-2">📱</span>
            <input id="input-phone" type="tel" value="+998 " placeholder="+998 90 123 45 67" class="bg-transparent text-sm w-full outline-none text-white font-mono" maxlength="20">
          </div>
          <span class="text-[11px] text-slate-500 mt-1.5 block">Kod Telegram ilovangizga ("Telegram" xizmat chatiga) yuboriladi</span>
        </div>
        <button id="btn-send-code" class="w-full py-3.5 rounded-xl font-bold text-sm bg-gradient-to-r from-red-600 to-rose-600 hover:from-red-500 hover:to-rose-500 text-white shadow-lg shadow-red-600/30 transition disabled:opacity-60">KODNI OLISH</button>
      </div>

      <div id="step-code" class="space-y-5 hidden">
        <div class="text-center">
          <div class="text-xs text-slate-400">Tasdiqlash kodi yuborildi:</div>
          <div id="display-phone" class="text-sm font-mono font-bold text-red-400 mt-0.5"></div>
        </div>
        <div>
          <label class="block text-xs font-semibold text-slate-300 mb-1.5 text-center">Tasdiqlash kodi</label>
          <input id="input-code" type="text" inputmode="numeric" autocomplete="one-time-code" placeholder="1 2 3 4 5" maxlength="6" class="w-full text-center tracking-[0.5em] text-2xl font-mono py-3 rounded-xl bg-slate-900 border border-slate-700 focus:border-red-500 outline-none text-white">
        </div>
        <button id="btn-verify-code" class="w-full py-3.5 rounded-xl font-bold text-sm bg-gradient-to-r from-red-600 to-rose-600 hover:from-red-500 text-white shadow-lg transition disabled:opacity-60">KIRISH</button>
        <button id="btn-back-phone" class="w-full py-2 text-xs text-slate-400 hover:text-slate-200 transition">← Boshqa raqam kiritish</button>
      </div>

      <div id="step-password" class="space-y-5 hidden">
        <div class="text-center text-xs text-slate-400">Hisobingizda ikki bosqichli himoya (2FA) yoqilgan. Parolni kiriting.</div>
        <input id="input-password" type="password" placeholder="Parol" autocomplete="current-password" class="w-full text-center text-base py-3 rounded-xl bg-slate-900 border border-slate-700 focus:border-red-500 outline-none text-white">
        <button id="btn-verify-password" class="w-full py-3.5 rounded-xl font-bold text-sm bg-gradient-to-r from-red-600 to-rose-600 hover:from-red-500 text-white shadow-lg transition disabled:opacity-60">TASDIQLASH</button>
        <button id="btn-back-phone-2" class="w-full py-2 text-xs text-slate-400 hover:text-slate-200 transition">← Boshqa raqam kiritish</button>
      </div>
    </div>
  </div>

  <!-- MAIN MESSENGER INTERFACE -->
  <div id="main-messenger" class="flex-1 flex overflow-hidden w-full h-full">
    <aside id="chat-sidebar" class="w-full sm:w-80 md:w-96 bg-[#0e121b] border-r border-slate-800 flex flex-col h-full z-20 flex-shrink-0">
      <div class="p-3.5 flex items-center justify-between border-b border-slate-800/80">
        <div class="flex items-center space-x-2">
          <div class="w-8 h-8 rounded-xl bg-red-600 flex items-center justify-center font-black text-xs text-white">RG</div>
          <span class="font-extrabold text-sm tracking-wide">RED<span class="text-red-500">gram</span></span>
        </div>
        <div class="flex items-center">
          <button id="btn-settings" class="p-2 rounded-xl text-slate-400 hover:text-red-400 hover:bg-slate-800 transition" title="Sozlamalar">⚙️</button>
          <button id="btn-logout" class="p-2 rounded-xl text-slate-400 hover:text-red-400 hover:bg-slate-800 transition" title="Chiqish">🚪</button>
        </div>
      </div>

      <div class="p-3">
        <div class="flex items-center bg-slate-900/90 rounded-xl px-3 py-2 border border-slate-800">
          <span class="text-slate-400 text-sm mr-2">🔍</span>
          <input id="chat-search" type="text" placeholder="Qidiruv..." class="bg-transparent text-xs w-full outline-none text-slate-200 select-text">
        </div>
      </div>

      <div class="flex px-3 pb-2 space-x-1 overflow-x-auto text-xs font-semibold border-b border-slate-800/60">
        <button class="folder-tab px-3 py-1.5 rounded-lg bg-red-600/20 text-red-400 border border-red-500/30 whitespace-nowrap" data-filter="all">Barchasi</button>
        <button class="folder-tab px-3 py-1.5 rounded-lg text-slate-400 hover:text-slate-200 whitespace-nowrap" data-filter="direct">Shaxsiy</button>
        <button class="folder-tab px-3 py-1.5 rounded-lg text-slate-400 hover:text-slate-200 whitespace-nowrap" data-filter="group">Guruhlar</button>
        <button class="folder-tab px-3 py-1.5 rounded-lg text-slate-400 hover:text-slate-200 whitespace-nowrap" data-filter="channel">Kanallar</button>
      </div>

      <div id="chats-list" class="flex-1 overflow-y-auto divide-y divide-slate-800/40"></div>
    </aside>

    <main id="chat-view" class="flex flex-1 flex-col hidden sm:flex bg-[#080b12] relative h-full min-w-0">
      <div id="chat-empty" class="flex flex-1 items-center justify-center text-slate-500 text-sm">Suhbatni tanlang</div>

      <div id="chat-pane" class="hidden flex flex-1 flex-col min-h-0">
        <header class="p-3 px-4 border-b border-slate-800 flex items-center justify-between bg-[#0e121b]/80 backdrop-blur z-10">
          <div class="flex items-center space-x-3 min-w-0">
            <button id="btn-back-to-list" class="sm:hidden p-1.5 rounded-lg bg-slate-800 text-slate-300">←</button>
            <div id="current-avatar" class="relative overflow-hidden flex-shrink-0 w-10 h-10 rounded-2xl bg-red-600 flex items-center justify-center font-bold text-sm text-white">RG</div>
            <div class="min-w-0">
              <div id="current-name" class="font-bold text-sm text-white truncate"></div>
              <div id="current-status" class="text-[11px] text-slate-400 font-medium"></div>
            </div>
          </div>
        </header>

        <div id="messages-container" class="flex-1 overflow-y-auto p-4 space-y-3 select-text"></div>

        <footer class="bg-[#0e121b] border-t border-slate-800">
          <div id="compose-bar" class="hidden"></div>
          <div class="p-3 flex items-end space-x-2">
            <button id="btn-attach" class="w-10 h-10 flex-shrink-0 rounded-2xl bg-slate-800 hover:bg-slate-700 text-slate-200 flex items-center justify-center transition" title="Fayl biriktirish">📎</button>
            <input id="file-input" type="file" class="hidden">
            <textarea id="input-message" rows="1" placeholder="Xabar yozing..." class="flex-1 bg-slate-900 rounded-2xl px-4 py-2.5 text-sm outline-none text-white border border-slate-800 focus:border-red-500 select-text resize-none max-h-32"></textarea>
            <button id="btn-send-message" class="w-10 h-10 flex-shrink-0 rounded-2xl bg-red-600 hover:bg-red-500 text-white flex items-center justify-center font-bold transition">➤</button>
          </div>
        </footer>
      </div>
    </main>
  </div>

  <!-- Xabar menyusi -->
  <div id="msg-menu" class="hidden fixed z-50 bg-[#161b26] border border-slate-700 rounded-xl shadow-2xl py-1 text-xs min-w-[170px]"></div>

  <!-- Rasm ko'rish -->
  <div id="lightbox" class="hidden fixed inset-0 z-50 bg-black/90 flex items-center justify-center p-3">
    <img id="lightbox-img" alt="" class="max-w-full max-h-full object-contain">
    <button class="absolute top-4 right-4 w-9 h-9 rounded-full bg-slate-800 text-white">✕</button>
  </div>

  <!-- Sozlamalar -->
  <div id="settings-modal" class="hidden fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4">
    <div class="w-full max-w-sm max-h-full overflow-y-auto bg-[#121622] border border-red-500/25 rounded-3xl p-5 space-y-5 select-text">
      <div class="flex items-center justify-between">
        <h2 class="font-extrabold text-base">Sozlamalar</h2>
        <button id="btn-settings-close" class="w-8 h-8 rounded-full bg-slate-800 text-slate-300">✕</button>
      </div>

      <div class="flex items-center space-x-3">
        <div id="set-avatar" class="relative overflow-hidden flex-shrink-0 w-14 h-14 rounded-2xl bg-red-600 flex items-center justify-center font-bold text-lg text-white">RG</div>
        <div class="min-w-0">
          <div id="set-name" class="font-bold text-sm truncate">...</div>
          <div id="set-username" class="text-xs text-red-400 truncate"></div>
          <div id="set-phone" class="text-xs text-slate-400 font-mono"></div>
        </div>
      </div>

      <div>
        <div class="text-xs font-semibold text-slate-300 mb-2">Xabar rangi</div>
        <div id="set-colors" class="flex space-x-2"></div>
      </div>

      <div>
        <div class="flex items-center justify-between text-xs font-semibold text-slate-300 mb-2"><span>Matn o'lchami</span><span id="set-fs-val" class="text-slate-400"></span></div>
        <input id="set-fs" type="range" min="11" max="18" step="1" class="w-full accent-red-500">
      </div>

      <label class="flex items-center justify-between text-xs font-semibold text-slate-300">
        <span>Enter bosilganda yuborish</span>
        <input id="set-enter" type="checkbox" class="w-4 h-4 accent-red-500">
      </label>
      <label class="flex items-center justify-between text-xs font-semibold text-slate-300">
        <span>Yangi xabar tovushi</span>
        <input id="set-sound" type="checkbox" class="w-4 h-4 accent-red-500">
      </label>

      <button id="btn-logout-2" class="w-full py-3 rounded-xl font-bold text-sm bg-slate-800 hover:bg-red-600/30 text-red-400 transition">Hisobdan chiqish</button>
    </div>
  </div>

  <script>
    // Frontend backenddan alohida joyda (masalan GitHub Pages) bo'lsa, server manzilini yozing:
    // window.REDGRAM_API = 'https://sizning-ilova.up.railway.app';
    window.REDGRAM_API = window.REDGRAM_API || '';
  </script>
  <script>
    const API = (window.REDGRAM_API || '').replace(/\/$/, '');
    const $ = (id) => document.getElementById(id);

    let token = localStorage.getItem('redgram_token') || '';
    let pendingToken = '';
    let chats = [];
    let currentChatId = null;
    let activeFilter = 'all';
    let messagesSig = '';
    let lastListSig = '';
    let msgTimer = null, dlgTimer = null;
    let loadingMsgs = false, loadingDlgs = false, dirtyMsgs = false, dirtyDlgs = false;
    let msgById = new Map();
    let replyTo = null, editing = null, pendingFile = null, asFile = false, sending = false;
    let stick = true;
    let meInfo = null;
    let ws = null, wsRetry = 0;

    const TYPE_LABEL = { direct: 'Shaxsiy chat', group: 'Guruh', channel: 'Kanal' };
    const COLORS = {
      red: ['#dc2626', '#b91c1c'], blue: ['#2563eb', '#1d4ed8'], green: ['#16a34a', '#15803d'],
      purple: ['#9333ea', '#7e22ce'], orange: ['#ea580c', '#c2410c']
    };
    let settings = { color: 'red', fs: 12, enterSend: true, sound: true };
    try { Object.assign(settings, JSON.parse(localStorage.getItem('redgram_settings') || '{}')); } catch (e) {}

    function esc(s) {
      return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
    function hashStr(s) { let h = 5381; for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0; return h; }
    function fmtSize(n) {
      if (!n) return '';
      if (n < 1024) return n + ' B';
      if (n < 1048576) return (n / 1024).toFixed(0) + ' KB';
      return (n / 1048576).toFixed(1) + ' MB';
    }

    async function api(path, { method = 'GET', body, form } = {}) {
      const headers = {};
      if (token) headers['X-Token'] = token;
      if (body) headers['Content-Type'] = 'application/json';
      let res;
      try {
        res = await fetch(API + path, { method, headers, body: form || (body ? JSON.stringify(body) : undefined) });
      } catch (e) {
        throw new Error("Serverga ulanib bo'lmadi");
      }
      let data = {};
      try { data = await res.json(); } catch (e) {}
      if (!res.ok) {
        const er = new Error(data.error || 'Xatolik yuz berdi');
        er.status = res.status;
        throw er;
      }
      return data;
    }
    const mediaUrl = (id, extra) => `${API}/api/media?chat_id=${currentChatId}&msg_id=${id}&t=${encodeURIComponent(token)}${extra || ''}`;

    // ---------- sozlamalar ----------
    function applySettings() {
      const c = COLORS[settings.color] || COLORS.red;
      const r = document.documentElement.style;
      r.setProperty('--b1', c[0]); r.setProperty('--b2', c[1]); r.setProperty('--fs', settings.fs + 'px');
    }
    function saveSettings() { localStorage.setItem('redgram_settings', JSON.stringify(settings)); applySettings(); }
    applySettings();

    function renderColorButtons() {
      $('set-colors').innerHTML = Object.entries(COLORS).map(([k, c]) =>
        `<button data-color="${k}" class="w-8 h-8 rounded-full border-2 ${settings.color === k ? 'border-white' : 'border-transparent'}" style="background:linear-gradient(135deg,${c[0]},${c[1]})"></button>`).join('');
    }
    async function openSettings() {
      renderColorButtons();
      $('set-fs').value = settings.fs; $('set-fs-val').innerText = settings.fs + 'px';
      $('set-enter').checked = settings.enterSend; $('set-sound').checked = settings.sound;
      $('settings-modal').classList.remove('hidden');
      try {
        if (!meInfo) meInfo = await api('/api/me');
        $('set-name').innerText = meInfo.name || '—';
        $('set-username').innerText = meInfo.username ? '@' + meInfo.username : '';
        $('set-phone').innerText = meInfo.phone ? '+' + meInfo.phone : '';
        $('set-avatar').innerHTML = avatarInner({ id: meInfo.id, name: meInfo.name });
      } catch (e) {}
    }
    $('btn-settings').addEventListener('click', openSettings);
    $('btn-settings-close').addEventListener('click', () => $('settings-modal').classList.add('hidden'));
    $('settings-modal').addEventListener('click', (e) => { if (e.target === $('settings-modal')) $('settings-modal').classList.add('hidden'); });
    $('set-colors').addEventListener('click', (e) => {
      const b = e.target.closest('[data-color]'); if (!b) return;
      settings.color = b.dataset.color; saveSettings(); renderColorButtons(); messagesSig = ''; loadMessages();
    });
    $('set-fs').addEventListener('input', (e) => { settings.fs = Number(e.target.value); $('set-fs-val').innerText = settings.fs + 'px'; saveSettings(); });
    $('set-enter').addEventListener('change', (e) => { settings.enterSend = e.target.checked; saveSettings(); });
    $('set-sound').addEventListener('change', (e) => { settings.sound = e.target.checked; saveSettings(); });
    $('btn-logout-2').addEventListener('click', () => { $('settings-modal').classList.add('hidden'); if (confirm('Hisobdan chiqmoqchimisiz?')) doLogout(false); });

    let actx = null;
    function beep() {
      if (!settings.sound) return;
      try {
        actx = actx || new (window.AudioContext || window.webkitAudioContext)();
        const o = actx.createOscillator(), g = actx.createGain();
        o.connect(g); g.connect(actx.destination);
        o.frequency.value = 880;
        g.gain.setValueAtTime(0.08, actx.currentTime);
        g.gain.exponentialRampToValueAtTime(0.0001, actx.currentTime + 0.25);
        o.start(); o.stop(actx.currentTime + 0.25);
      } catch (e) {}
    }

    // ---------- vaqt va avatar ----------
    function pad(n) { return String(n).padStart(2, '0'); }
    function hhmm(ts) { const d = new Date(ts * 1000); return pad(d.getHours()) + ':' + pad(d.getMinutes()); }
    function fmtTime(ts) {
      if (!ts) return '';
      const d = new Date(ts * 1000), n = new Date();
      if (d.toDateString() === n.toDateString()) return hhmm(ts);
      if ((n - d) / 86400000 < 7) return ['Yak', 'Dush', 'Sesh', 'Chor', 'Pay', 'Jum', 'Shan'][d.getDay()];
      return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + '.' + String(d.getFullYear()).slice(2);
    }
    function initials(name) {
      const parts = String(name || '?').trim().split(/\s+/).slice(0, 2);
      return parts.map((p) => Array.from(p)[0] || '').join('').toUpperCase() || '?';
    }
    function avatarInner(chat) {
      return `<span>${esc(initials(chat.name))}</span>` +
        `<img loading="lazy" alt="" src="${API}/api/avatar/${chat.id}?t=${encodeURIComponent(token)}" ` +
        `style="opacity:0;transition:opacity .2s" onload="this.style.opacity=1" onerror="this.remove()" ` +
        `class="absolute inset-0 w-full h-full object-cover">`;
    }

    // ---------- kirish ----------
    function showAuthError(msg) { const e = $('auth-error'); e.textContent = msg; e.classList.remove('hidden'); }
    function hideAuthError() { $('auth-error').classList.add('hidden'); }
    function showStep(id) {
      ['step-phone', 'step-code', 'step-password'].forEach((s) => $(s).classList.toggle('hidden', s !== id));
      hideAuthError();
    }
    async function withBusy(btn, busyText, fn) {
      const label = btn.textContent;
      btn.disabled = true; btn.textContent = busyText; hideAuthError();
      try { await fn(); } catch (e) { showAuthError(e.message); }
      finally { btn.disabled = false; btn.textContent = label; }
    }

    $('btn-send-code').addEventListener('click', () => withBusy($('btn-send-code'), 'YUBORILMOQDA...', async () => {
      const digits = $('input-phone').value.replace(/\D/g, '');
      if (digits.length < 9) throw new Error("Telefon raqamni to'liq kiriting (masalan +998 90 123 45 67)");
      const phone = '+' + digits;
      const r = await api('/api/send_code', { method: 'POST', body: { phone } });
      pendingToken = r.token;
      $('display-phone').innerText = phone;
      $('input-code').value = '';
      showStep('step-code');
      $('input-code').focus();
    }));
    $('btn-verify-code').addEventListener('click', () => withBusy($('btn-verify-code'), 'TEKSHIRILMOQDA...', async () => {
      const code = $('input-code').value.replace(/\D/g, '');
      if (code.length < 5) throw new Error("Kodni to'liq kiriting");
      const r = await api('/api/sign_in', { method: 'POST', body: { token: pendingToken, code } });
      if (r.need_password) { showStep('step-password'); $('input-password').focus(); return; }
      await finishLogin(pendingToken);
    }));
    $('btn-verify-password').addEventListener('click', () => withBusy($('btn-verify-password'), 'TEKSHIRILMOQDA...', async () => {
      const password = $('input-password').value;
      if (!password) throw new Error('Parolni kiriting');
      await api('/api/sign_in', { method: 'POST', body: { token: pendingToken, password } });
      await finishLogin(pendingToken);
    }));
    function backToPhone() { pendingToken = ''; showStep('step-phone'); }
    $('btn-back-phone').addEventListener('click', backToPhone);
    $('btn-back-phone-2').addEventListener('click', backToPhone);
    $('input-phone').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('btn-send-code').click(); });
    $('input-code').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('btn-verify-code').click(); });
    $('input-password').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('btn-verify-password').click(); });

    async function finishLogin(t) {
      token = t;
      localStorage.setItem('redgram_token', t);
      pendingToken = ''; meInfo = null;
      $('input-code').value = ''; $('input-password').value = '';
      $('auth-screen').classList.add('hidden');
      await startApp();
    }
    async function doLogout(silent) {
      if (!silent && token) { try { await api('/api/logout', { method: 'POST' }); } catch (e) {} }
      closeWS();
      token = ''; meInfo = null;
      localStorage.removeItem('redgram_token');
      stopPolling();
      chats = []; currentChatId = null; messagesSig = ''; lastListSig = '';
      replyTo = null; editing = null; pendingFile = null; renderComposeBar();
      $('chats-list').innerHTML = '';
      $('chat-sidebar').classList.remove('hidden');
      $('chat-view').classList.add('hidden');
      showPane(false);
      showStep('step-phone');
      $('auth-screen').classList.remove('hidden');
    }
    $('btn-logout').addEventListener('click', () => { if (confirm('Hisobdan chiqmoqchimisiz?')) doLogout(false); });

    // ---------- chatlar ----------
    async function loadDialogs() {
      if (loadingDlgs) { dirtyDlgs = true; return; }
      loadingDlgs = true;
      try {
        const r = await api('/api/dialogs');
        chats = r.dialogs;
        renderChatsList();
      } catch (e) {
        if (e.status === 401) doLogout(true);
      } finally {
        loadingDlgs = false;
        if (dirtyDlgs) { dirtyDlgs = false; loadDialogs(); }
      }
    }

    function renderChatsList() {
      const container = $('chats-list');
      const query = $('chat-search').value.toLowerCase();
      const filtered = chats.filter((c) => (activeFilter === 'all' || c.type === activeFilter) && c.name.toLowerCase().includes(query));
      const sig = JSON.stringify([filtered, currentChatId, activeFilter, query, token]);
      if (sig === lastListSig) return;
      lastListSig = sig;
      container.innerHTML = '';
      if (!filtered.length) {
        container.innerHTML = '<div class="p-6 text-center text-xs text-slate-500">Chatlar topilmadi</div>';
        return;
      }
      filtered.forEach((chat) => {
        const isSelected = chat.id === currentChatId;
        const item = document.createElement('div');
        item.className = `p-3 px-3.5 flex items-center space-x-3 cursor-pointer transition ${isSelected ? 'bg-red-950/40 border-l-4 border-red-500' : 'hover:bg-slate-900/60'}`;
        item.innerHTML = `
          <div class="relative overflow-hidden flex-shrink-0 w-11 h-11 rounded-2xl bg-red-600 flex items-center justify-center font-bold text-sm text-white">${avatarInner(chat)}</div>
          <div class="flex-1 min-w-0">
            <div class="flex items-center justify-between"><h4 class="font-bold text-xs truncate text-white">${esc(chat.name)}</h4><span class="text-[10px] text-slate-400 ml-2 flex-shrink-0">${esc(fmtTime(chat.date))}</span></div>
            <div class="flex items-center justify-between mt-1">
              <p class="text-xs text-slate-400 truncate">${esc(chat.last)}</p>
              ${chat.unread ? `<span class="ml-2 flex-shrink-0 bg-red-600 text-white text-[10px] font-bold rounded-full px-1.5 min-w-[18px] text-center">${chat.unread}</span>` : ''}
            </div>
          </div>`;
        item.addEventListener('click', () => selectChat(chat.id));
        container.appendChild(item);
      });
    }

    function showPane(on) {
      $('chat-pane').classList.toggle('hidden', !on);
      $('chat-empty').classList.toggle('hidden', on);
    }

    async function selectChat(id) {
      const chat = chats.find((c) => c.id === id);
      if (!chat) return;
      currentChatId = id;
      messagesSig = ''; stick = true;
      replyTo = null; editing = null; pendingFile = null; $('file-input').value = '';
      $('input-message').value = ''; $('input-message').style.height = 'auto';
      renderComposeBar();
      $('current-name').innerText = chat.name;
      $('current-status').innerText = TYPE_LABEL[chat.type] || '';
      $('current-avatar').innerHTML = avatarInner(chat);
      $('messages-container').innerHTML = '<div class="text-center text-xs text-slate-500 py-6">Yuklanmoqda...</div>';
      showPane(true);
      renderChatsList();
      $('chat-sidebar').classList.add('hidden', 'sm:flex');
      $('chat-view').classList.remove('hidden');
      await loadMessages();
      $('input-message').focus();
    }

    async function loadMessages() {
      if (currentChatId === null) return;
      if (loadingMsgs) { dirtyMsgs = true; return; }
      const id = currentChatId;
      loadingMsgs = true;
      try {
        const r = await api('/api/messages?chat_id=' + encodeURIComponent(id));
        if (id !== currentChatId) return;
        const sig = r.messages.map((m) => m.id + ':' + hashStr(m.text) + (m.edited ? 'e' : '')).join(',');
        if (sig === messagesSig) return;
        const first = messagesSig === '';
        messagesSig = sig;
        renderMessages(r.messages, first);
      } catch (e) {
        if (e.status === 401) doLogout(true);
        else if (messagesSig === '') $('messages-container').innerHTML = `<div class="text-center text-xs text-red-300 py-6">${esc(e.message)}</div>`;
      } finally {
        loadingMsgs = false;
        if (dirtyMsgs) { dirtyMsgs = false; loadMessages(); }
      }
    }

    function mediaHtml(msg) {
      const url = mediaUrl(msg.id);
      const fb = esc(msg.media || 'Media');
      switch (msg.kind) {
        case 'photo':
          return `<img class="msg-img rounded-xl mb-1 max-w-[260px] max-h-72 object-cover cursor-pointer" loading="lazy" src="${mediaUrl(msg.id, '&thumb=1')}" data-full="${url}" data-fallback="📷 Rasm" alt="">`;
        case 'sticker':
          return `<img class="w-32 h-32 object-contain" loading="lazy" src="${url}" data-fallback="${fb}" alt="">`;
        case 'gif':
          return `<video class="rounded-xl mb-1 max-w-[260px] max-h-72" src="${url}" autoplay loop muted playsinline data-fallback="GIF"></video>`;
        case 'video':
          return `<video class="rounded-xl mb-1 max-w-[260px] max-h-72" src="${url}" poster="${mediaUrl(msg.id, '&thumb=1')}" controls preload="none" playsinline data-fallback="🎬 Video"></video>`;
        case 'voice':
        case 'audio':
          return `<audio class="max-w-[240px] mb-1" src="${url}" controls preload="none" data-fallback="${fb}"></audio>` +
            (msg.kind === 'audio' && msg.name ? `<div class="opacity-80 mb-1">${esc(msg.name)}</div>` : '');
        case 'file':
          return `<a href="${url}&dl=1" target="_blank" rel="noopener" class="flex items-center space-x-2 bg-black/20 rounded-xl px-3 py-2 mb-1 max-w-[260px]"><span class="text-lg">📎</span><span class="min-w-0"><span class="block truncate font-semibold">${esc(msg.name || 'Fayl')}</span><span class="block opacity-70 text-[10px]">${esc(fmtSize(msg.size))} · yuklab olish</span></span></a>`;
        default:
          return msg.media ? `<p class="opacity-80 italic">${esc(msg.media)}</p>` : '';
      }
    }

    function renderMessages(list, first) {
      const container = $('messages-container');
      const chat = chats.find((c) => c.id === currentChatId);
      const isGroup = chat && chat.type === 'group';
      msgById = new Map(list.map((m) => [m.id, m]));
      if (first) stick = true;
      container.innerHTML = '';
      if (!list.length) {
        container.innerHTML = '<div class="text-center text-xs text-slate-500 py-6">Xabarlar yo\'q</div>';
        return;
      }
      list.forEach((msg) => {
        const wrap = document.createElement('div');
        wrap.className = `flex ${msg.out ? 'justify-end' : 'justify-start'}`;
        wrap.dataset.id = msg.id;
        const who = isGroup && !msg.out && msg.sender ? `<div class="text-[11px] font-bold text-red-400 mb-0.5">${esc(msg.sender)}</div>` : '';
        const fwd = msg.fwd ? '<div class="text-[10px] opacity-70 italic mb-0.5">↪ Forward qilingan</div>' : '';
        const reply = msg.reply ? `<div class="border-l-2 border-white/60 pl-2 mb-1 opacity-90"><div class="font-bold text-[11px] truncate">${esc(msg.reply.sender || 'Xabar')}</div><div class="truncate text-[11px]">${esc(msg.reply.text)}</div></div>` : '';
        const media = mediaHtml(msg);
        const text = msg.text ? `<p class="whitespace-pre-wrap break-words">${esc(msg.text)}</p>` : '';
        const body = media || text ? media + text : '<p class="opacity-60 italic">Qo\'llab-quvvatlanmaydigan xabar</p>';
        wrap.innerHTML = `<div class="max-w-[80%] px-3 py-2 rounded-2xl ${msg.out ? 'bubble-sent text-white' : 'bubble-recv text-slate-200'} shadow cursor-pointer">${who}${fwd}${reply}${body}<div class="text-[10px] text-right mt-1 opacity-70">${msg.edited ? 'tahrirlangan · ' : ''}${hhmm(msg.date)} ⋯</div></div>`;
        container.appendChild(wrap);
      });
      if (stick) container.scrollTop = container.scrollHeight;
    }

    const msgBox = $('messages-container');
    msgBox.addEventListener('scroll', () => { stick = msgBox.scrollHeight - msgBox.scrollTop - msgBox.clientHeight < 80; });
    msgBox.addEventListener('load', () => { if (stick) msgBox.scrollTop = msgBox.scrollHeight; }, true);
    msgBox.addEventListener('error', (e) => {
      const t = e.target;
      if (t && t.dataset && t.dataset.fallback) {
        const p = document.createElement('p');
        p.className = 'opacity-80 italic';
        p.textContent = t.dataset.fallback;
        t.replaceWith(p);
      }
    }, true);
    msgBox.addEventListener('click', (e) => {
      const img = e.target.closest('img.msg-img');
      if (img) { $('lightbox-img').src = img.dataset.full; $('lightbox').classList.remove('hidden'); return; }
      if (e.target.closest('a,audio,video')) return;
      const row = e.target.closest('[data-id]');
      if (row) openMenu(Number(row.dataset.id), e);
    });
    $('lightbox').addEventListener('click', () => { $('lightbox').classList.add('hidden'); $('lightbox-img').src = ''; });

    // ---------- xabar menyusi ----------
    function openMenu(id, ev) {
      const m = msgById.get(id); if (!m) return;
      const items = [['reply', '↩ Javob berish']];
      if (m.text) items.push(['copy', '📋 Nusxa olish']);
      if (m.out && m.text) items.push(['edit', '✏️ Tahrirlash']);
      items.push(['delete', "🗑 O'chirish"]);
      const menu = $('msg-menu');
      menu.innerHTML = items.map(([k, t]) => `<button data-act="${k}" class="block w-full text-left px-4 py-2 hover:bg-slate-800 ${k === 'delete' ? 'text-red-400' : 'text-slate-200'}">${t}</button>`).join('');
      menu.dataset.id = id;
      menu.classList.remove('hidden');
      const x = Math.min(ev.clientX, innerWidth - menu.offsetWidth - 8);
      const y = Math.min(ev.clientY, innerHeight - menu.offsetHeight - 8);
      menu.style.left = Math.max(8, x) + 'px';
      menu.style.top = Math.max(8, y) + 'px';
      ev.stopPropagation();
    }
    document.addEventListener('click', () => $('msg-menu').classList.add('hidden'));
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { $('msg-menu').classList.add('hidden'); $('lightbox').click(); $('settings-modal').classList.add('hidden'); }
    });
    $('msg-menu').addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-act]'); if (!btn) return;
      e.stopPropagation();
      const menu = $('msg-menu'); menu.classList.add('hidden');
      const m = msgById.get(Number(menu.dataset.id)); if (!m) return;
      const input = $('input-message');
      if (btn.dataset.act === 'reply') {
        const chat = chats.find((c) => c.id === currentChatId);
        replyTo = { id: m.id, text: m.text || m.media || 'Media', sender: m.out ? 'Siz' : (m.sender || (chat && chat.name) || '') };
        editing = null; renderComposeBar(); input.focus();
      } else if (btn.dataset.act === 'copy') {
        try { await navigator.clipboard.writeText(m.text); }
        catch (er) { const t = document.createElement('textarea'); t.value = m.text; document.body.appendChild(t); t.select(); document.execCommand('copy'); t.remove(); }
      } else if (btn.dataset.act === 'edit') {
        editing = { id: m.id, text: m.text }; replyTo = null; pendingFile = null; $('file-input').value = '';
        input.value = m.text; autoGrow(); renderComposeBar(); input.focus();
      } else if (btn.dataset.act === 'delete') {
        if (!confirm("Xabarni o'chirmoqchimisiz?")) return;
        try {
          await api('/api/delete', { method: 'POST', body: { chat_id: currentChatId, msg_id: m.id } });
          messagesSig = ''; await loadMessages(); loadDialogs();
        } catch (er) { alert(er.message); }
      }
    });

    // ---------- yozish paneli ----------
    function renderComposeBar() {
      const bar = $('compose-bar');
      const rows = [];
      if (editing) rows.push(['edit', '✏️ Tahrirlash', editing.text]);
      if (replyTo && !editing) rows.push(['reply', '↩ Javob: ' + (replyTo.sender || ''), replyTo.text]);
      if (pendingFile && !editing) rows.push(['file', '📎 ' + pendingFile.name, fmtSize(pendingFile.size)]);
      bar.classList.toggle('hidden', !rows.length);
      let html = rows.map(([k, t, s]) => `<div class="flex items-center justify-between px-3 py-1.5 text-xs border-l-2 border-red-500 bg-slate-900/60"><div class="min-w-0"><div class="font-bold text-red-400 truncate">${esc(t)}</div><div class="text-slate-400 truncate">${esc(s)}</div></div><button data-clear="${k}" class="px-2 text-slate-400 hover:text-white">✕</button></div>`).join('');
      if (pendingFile && !editing && pendingFile.type.startsWith('image/')) {
        html += `<label class="flex items-center space-x-2 px-3 py-1.5 text-[11px] text-slate-300"><input type="checkbox" id="as-file" ${asFile ? 'checked' : ''}><span>Fayl sifatida yuborish (siqilmasdan)</span></label>`;
      }
      bar.innerHTML = html;
    }
    $('compose-bar').addEventListener('click', (e) => {
      const b = e.target.closest('[data-clear]'); if (!b) return;
      if (b.dataset.clear === 'edit') { editing = null; $('input-message').value = ''; autoGrow(); }
      if (b.dataset.clear === 'reply') replyTo = null;
      if (b.dataset.clear === 'file') { pendingFile = null; $('file-input').value = ''; }
      renderComposeBar();
    });
    $('compose-bar').addEventListener('change', (e) => { if (e.target.id === 'as-file') asFile = e.target.checked; });

    $('btn-attach').addEventListener('click', () => $('file-input').click());
    $('file-input').addEventListener('change', (e) => {
      const f = e.target.files[0]; if (!f) return;
      if (f.size > 30 * 1024 * 1024) { alert('Fayl juda katta (30 MB gacha)'); e.target.value = ''; return; }
      pendingFile = f; asFile = false; editing = null; renderComposeBar();
    });

    function autoGrow() { const el = $('input-message'); el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 128) + 'px'; }
    $('input-message').addEventListener('input', autoGrow);
    $('input-message').addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      const doSend = settings.enterSend ? !e.shiftKey : (e.ctrlKey || e.metaKey);
      if (doSend) { e.preventDefault(); sendMessage(); }
    });

    async function sendMessage() {
      if (sending || currentChatId === null) return;
      const input = $('input-message');
      const text = input.value.trim();
      sending = true;
      try {
        if (editing) {
          if (!text) return;
          await api('/api/edit', { method: 'POST', body: { chat_id: currentChatId, msg_id: editing.id, text } });
          editing = null;
        } else if (pendingFile) {
          $('btn-send-message').textContent = '⏳';
          const fd = new FormData();
          fd.append('chat_id', currentChatId);
          fd.append('caption', text);
          if (replyTo) fd.append('reply_to', replyTo.id);
          if (asFile) fd.append('as_file', '1');
          fd.append('file', pendingFile, pendingFile.name);
          await api('/api/upload', { method: 'POST', form: fd });
          pendingFile = null; $('file-input').value = ''; replyTo = null;
        } else {
          if (!text) return;
          await api('/api/send', { method: 'POST', body: { chat_id: currentChatId, text, reply_to: replyTo ? replyTo.id : null } });
          replyTo = null;
        }
        input.value = ''; autoGrow(); renderComposeBar();
        messagesSig = ''; stick = true;
        await loadMessages(); loadDialogs();
      } catch (e) {
        alert(e.message);
      } finally {
        sending = false;
        $('btn-send-message').textContent = '➤';
      }
    }
    $('btn-send-message').addEventListener('click', sendMessage);
    $('chat-search').addEventListener('input', renderChatsList);

    document.querySelectorAll('.folder-tab').forEach((tab) => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.folder-tab').forEach((t) => t.className = 'folder-tab px-3 py-1.5 rounded-lg text-slate-400 hover:text-slate-200 whitespace-nowrap');
        tab.className = 'folder-tab px-3 py-1.5 rounded-lg bg-red-600/20 text-red-400 border border-red-500/30 whitespace-nowrap';
        activeFilter = tab.dataset.filter;
        renderChatsList();
      });
    });
    $('btn-back-to-list').addEventListener('click', () => {
      currentChatId = null; messagesSig = '';
      showPane(false);
      $('chat-sidebar').classList.remove('hidden');
      $('chat-view').classList.add('hidden');
      renderChatsList();
    });

    // ---------- real vaqt (WebSocket) va zaxira so'rovlar ----------
    function closeWS() {
      if (ws) { ws.onclose = null; try { ws.close(); } catch (e) {} ws = null; }
    }
    function connectWS() {
      if (!token) return;
      closeWS();
      const used = token;
      const base = (API || location.origin).replace(/^http/, 'ws');
      try { ws = new WebSocket(base + '/api/ws?t=' + encodeURIComponent(used)); } catch (e) { return; }
      ws.onopen = () => { wsRetry = 0; };
      ws.onmessage = (ev) => {
        let d; try { d = JSON.parse(ev.data); } catch (e) { return; }
        loadDialogs();
        if (d.chat_id == null || d.chat_id === currentChatId) loadMessages();
        if (!d.out && d.event === 'NewMessage' && (document.hidden || d.chat_id !== currentChatId)) beep();
      };
      ws.onclose = () => { ws = null; if (token && token === used) setTimeout(() => { if (token === used && !ws) connectWS(); }, Math.min(15000, 1000 * (++wsRetry))); };
    }
    function startPolling() {
      stopPolling();
      msgTimer = setInterval(() => { if (!document.hidden) loadMessages(); }, 12000);
      dlgTimer = setInterval(() => { if (!document.hidden) loadDialogs(); }, 20000);
    }
    function stopPolling() { clearInterval(msgTimer); clearInterval(dlgTimer); }
    document.addEventListener('visibilitychange', () => { if (!document.hidden && token) { loadDialogs(); loadMessages(); } });

    async function startApp() {
      $('chats-list').innerHTML = '<div class="p-6 text-center text-xs text-slate-500">Yuklanmoqda...</div>';
      showPane(false);
      await loadDialogs();
      startPolling();
      connectWS();
    }

    // ---------- boshlanish ----------
    (async () => {
      if (!token) return;
      try {
        await api('/api/me');
        $('auth-screen').classList.add('hidden');
        await startApp();
      } catch (e) {
        if (e.status === 401) { token = ''; localStorage.removeItem('redgram_token'); }
        else showAuthError(e.message);
      }
    })();
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------- yordamchilar
def ok(data=None):
    return web.json_response({"ok": True, **(data or {})})


def err(message, status=400):
    return web.json_response({"error": message}, status=status)


def unauthorized():
    return web.HTTPUnauthorized(
        text=json.dumps({"error": "Kirish talab qilinadi"}),
        content_type="application/json",
    )


async def read_json(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def make_client(session=""):
    return TelegramClient(
        StringSession(session), API_ID, API_HASH,
        device_model="REDgram Web", app_version="1.0", system_version="Web",
    )


def save_sessions():
    data = {t: c.session.save() for t, c in clients.items()}
    tmp = SESSIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, SESSIONS_FILE)


def get_client(request):
    token = request.headers.get("X-Token") or request.query.get("t")
    client = clients.get(token)
    if not client:
        raise unauthorized()
    return token, client


async def get_entity(client, chat_id):
    try:
        return await client.get_input_entity(chat_id)
    except ValueError:
        # server qayta ishga tushgandan keyin entity keshi bo'sh — yangilaymiz
        await client.get_dialogs(limit=200)
        return await client.get_input_entity(chat_id)


def media_label(m):
    if m is None:
        return ""
    if getattr(m, "action", None):
        return "ℹ️ Xizmat xabari"
    if m.photo:
        return "📷 Rasm"
    if m.voice:
        return "🎤 Ovozli xabar"
    if m.video_note:
        return "⏺ Video xabar"
    if m.gif:
        return "GIF"
    if m.video:
        return "🎬 Video"
    if m.sticker:
        return "Stiker " + (getattr(m.file, "emoji", "") or "")
    if m.audio:
        return "🎵 Audio"
    if m.document:
        return "📎 Fayl"
    if m.poll:
        return "📊 So'rovnoma"
    if m.geo:
        return "📍 Joylashuv"
    if m.contact:
        return "👤 Kontakt"
    return ""


def preview(m):
    if m is None:
        return ""
    label = media_label(m)
    text = (m.message or "").replace("\n", " ")
    out = f"{label} {text}".strip()
    return out[:90]


def sender_name(m):
    s = m.sender
    if s is None:
        return ""
    first = getattr(s, "first_name", None) or ""
    last = getattr(s, "last_name", None) or ""
    name = f"{first} {last}".strip()
    return name or getattr(s, "title", "") or ""


def media_info(m):
    """(kind, nom, hajm, mime) — kind: photo|video|gif|voice|audio|sticker|file|label|none"""
    if m.photo:
        return "photo", "photo.jpg", 0, "image/jpeg"
    f = m.file
    name = (getattr(f, "name", None) or "") if f else ""
    size = (getattr(f, "size", 0) or 0) if f else 0
    mime = (getattr(f, "mime_type", "") or "") if f else ""
    if m.sticker:
        kind = "sticker" if mime == "image/webp" else "label"
    elif m.voice:
        kind = "voice"
    elif m.gif:
        kind = "gif"
    elif m.video_note or m.video:
        kind = "video"
    elif m.audio:
        kind = "audio"
    elif m.document:
        kind = "file"
    else:
        kind = "none"
    return kind, name, size, mime


def cache_put(key, entry):
    global media_bytes
    media_cache[key] = entry
    media_bytes += len(entry[0])
    while media_bytes > MAX_CACHE and len(media_cache) > 1:
        _, old = media_cache.popitem(last=False)
        media_bytes -= len(old[0])


def attach_updates(token, client):
    """Telegramdan yangilanish kelganda brauzerga WebSocket orqali xabar beradi."""
    async def on_update(event):
        msg = getattr(event, "message", None)
        payload = {
            "type": "update",
            "event": type(event).__name__,
            "chat_id": getattr(event, "chat_id", None),
            "out": bool(getattr(msg, "out", False)),
        }
        for ws in list(sockets.get(token, ())):
            try:
                await ws.send_json(payload)
            except Exception:
                sockets[token].discard(ws)

    client.add_event_handler(on_update, events.NewMessage())
    client.add_event_handler(on_update, events.MessageEdited())
    client.add_event_handler(on_update, events.MessageDeleted())


# ---------------------------------------------------------------- middleware
@web.middleware
async def cors_mw(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as ex:
            resp = ex
    resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Token"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@web.middleware
async def errors_mw(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except FloodWaitError as e:
        return err(f"Juda ko'p urinish. {e.seconds} soniyadan keyin qayta urining", 429)
    except Exception as e:
        name = type(e).__name__
        if name in ERROR_TEXTS:
            return err(ERROR_TEXTS[name])
        log.exception("Xatolik: %s", request.path)
        return err(f"Server xatosi: {name}", 500)


# ---------------------------------------------------------------- kirish
async def send_code(request):
    data = await read_json(request)
    phone = "+" + "".join(ch for ch in str(data.get("phone", "")) if ch.isdigit())
    if len(phone) < 10:
        return err("Telefon raqamni to'liq kiriting")

    # eskirgan kutilayotgan sessiyalarni tozalash
    now = time.time()
    for k in [k for k, v in pending.items() if now - v["ts"] > 600]:
        try:
            await pending.pop(k)["client"].disconnect()
        except Exception:
            pass

    client = make_client()
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
    except Exception:
        await client.disconnect()
        raise

    token = secrets.token_urlsafe(32)
    pending[token] = {"client": client, "phone": phone, "hash": sent.phone_code_hash, "ts": now}
    return ok({"token": token})


async def sign_in(request):
    data = await read_json(request)
    p = pending.get(data.get("token"))
    if not p:
        return err("Sessiya topilmadi, qaytadan kod oling")
    client = p["client"]
    password = data.get("password")
    code = "".join(ch for ch in str(data.get("code", "")) if ch.isdigit())

    try:
        if p.get("need_password") and password:
            await client.sign_in(password=password)
        else:
            await client.sign_in(p["phone"], code, phone_code_hash=p["hash"])
    except SessionPasswordNeededError:
        p["need_password"] = True
        return ok({"need_password": True})

    token = data["token"]
    pending.pop(token, None)
    clients[token] = client
    attach_updates(token, client)
    save_sessions()
    asyncio.create_task(client.get_dialogs(limit=100))  # entity keshini isitish
    return ok({"token": token})


async def me(request):
    _, client = get_client(request)
    u = await client.get_me()
    name = f"{u.first_name or ''} {u.last_name or ''}".strip()
    return ok({"id": u.id, "name": name, "username": u.username, "phone": u.phone})


async def logout(request):
    token, client = get_client(request)
    try:
        await client.log_out()
    except Exception:
        pass
    clients.pop(token, None)
    for ws in list(sockets.pop(token, ())):
        try:
            await ws.close()
        except Exception:
            pass
    save_sessions()
    return ok()


# ---------------------------------------------------------------- chatlar
async def dialogs(request):
    _, client = get_client(request)
    result = []
    for d in await client.get_dialogs(limit=60):
        if d.is_user:
            kind = "direct"
        elif d.is_group:
            kind = "group"
        else:
            kind = "channel"
        result.append({
            "id": d.id,
            "name": d.name or "Nomsiz",
            "type": kind,
            "last": preview(d.message),
            "date": int(d.date.timestamp()) if d.date else 0,
            "unread": d.unread_count,
        })
    return ok({"dialogs": result})


async def messages(request):
    token, client = get_client(request)
    try:
        chat_id = int(request.query["chat_id"])
    except (KeyError, ValueError):
        return err("chat_id noto'g'ri")

    entity = await get_entity(client, chat_id)
    msgs = await client.get_messages(entity, limit=50)

    if msgs and not msgs[0].out and last_read.get((token, chat_id)) != msgs[0].id:
        last_read[(token, chat_id)] = msgs[0].id
        try:
            await client.send_read_acknowledge(entity, msgs[0])
        except Exception:
            pass

    def reply_id(m):
        r = m.reply_to
        return getattr(r, "reply_to_msg_id", None) if r else None

    by_id = {m.id: m for m in msgs}
    missing = list({reply_id(m) for m in msgs if reply_id(m)} - set(by_id))
    if missing:
        try:
            for r in await client.get_messages(entity, ids=missing):
                if r:
                    by_id[r.id] = r
        except Exception:
            pass

    items = []
    for m in reversed(msgs):
        kind, name, size, mime = media_info(m)
        reply = None
        rid = reply_id(m)
        if rid:
            r = by_id.get(rid)
            reply = {"id": rid, "text": preview(r) if r else "Xabar",
                     "sender": sender_name(r) if r else ""}
        items.append({
            "id": m.id,
            "out": bool(m.out),
            "text": m.message or "",
            "media": media_label(m),
            "kind": kind,
            "mime": mime,
            "name": name,
            "size": size,
            "date": int(m.date.timestamp()),
            "sender": sender_name(m),
            "edited": bool(m.edit_date),
            "fwd": bool(m.fwd_from),
            "reply": reply,
        })
    return ok({"messages": items})


async def send(request):
    _, client = get_client(request)
    data = await read_json(request)
    text = str(data.get("text", "")).strip()
    if not text:
        return err("Xabar bo'sh")
    try:
        chat_id = int(data.get("chat_id"))
    except (TypeError, ValueError):
        return err("chat_id noto'g'ri")
    reply_to = data.get("reply_to")

    entity = await get_entity(client, chat_id)
    m = await client.send_message(entity, text, reply_to=int(reply_to) if reply_to else None)
    return ok({"id": m.id})


async def edit(request):
    _, client = get_client(request)
    data = await read_json(request)
    text = str(data.get("text", "")).strip()
    try:
        chat_id = int(data.get("chat_id"))
        msg_id = int(data.get("msg_id"))
    except (TypeError, ValueError):
        return err("Ma'lumot noto'g'ri")
    if not text:
        return err("Xabar bo'sh")
    entity = await get_entity(client, chat_id)
    await client.edit_message(entity, msg_id, text)
    return ok()


async def delete(request):
    _, client = get_client(request)
    data = await read_json(request)
    try:
        chat_id = int(data.get("chat_id"))
        msg_id = int(data.get("msg_id"))
    except (TypeError, ValueError):
        return err("Ma'lumot noto'g'ri")
    entity = await get_entity(client, chat_id)
    await client.delete_messages(entity, [msg_id])
    return ok()


async def upload(request):
    _, client = get_client(request)
    fields, content, filename = {}, None, "file"
    reader = await request.multipart()
    async for part in reader:
        if part.name == "file":
            filename = part.filename or "file"
            buf = bytearray()
            while True:
                chunk = await part.read_chunk(1 << 20)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > MAX_MEDIA:
                    return err("Fayl juda katta (30 MB gacha)", 413)
            content = bytes(buf)
        else:
            fields[part.name] = await part.text()
    if not content:
        return err("Fayl tanlanmagan")
    try:
        chat_id = int(fields.get("chat_id"))
    except (TypeError, ValueError):
        return err("chat_id noto'g'ri")

    entity = await get_entity(client, chat_id)
    bio = io.BytesIO(content)
    bio.name = filename
    kwargs = {"caption": (fields.get("caption") or "").strip() or None,
              "force_document": fields.get("as_file") == "1"}
    if fields.get("reply_to"):
        kwargs["reply_to"] = int(fields["reply_to"])
    m = await client.send_file(entity, bio, **kwargs)
    return ok({"id": m.id})


async def avatar(request):
    token, client = get_client(request)
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        raise web.HTTPNotFound()

    key = (token, chat_id)
    data = avatar_cache.get(key)
    if data is None:
        async with avatar_sem:
            try:
                entity = await get_entity(client, chat_id)
                data = await client.download_profile_photo(entity, file=bytes) or b""
            except Exception:
                data = b""
        avatar_cache[key] = data
    if not data:
        raise web.HTTPNotFound()
    return web.Response(body=data, content_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


async def media(request):
    token, client = get_client(request)
    try:
        chat_id = int(request.query["chat_id"])
        msg_id = int(request.query["msg_id"])
    except (KeyError, ValueError):
        return err("Ma'lumot noto'g'ri")
    thumb = request.query.get("thumb") == "1"
    dl = request.query.get("dl") == "1"

    key = (token, chat_id, msg_id, thumb)
    entry = media_cache.get(key)
    if entry is not None:
        media_cache.move_to_end(key)
    else:
        entity = await get_entity(client, chat_id)
        m = await client.get_messages(entity, ids=msg_id)
        if not m or not m.media:
            raise web.HTTPNotFound()
        kind, name, size, mime = media_info(m)
        if size > MAX_MEDIA:
            return err("Fayl juda katta (30 MB gacha)", 413)
        async with media_sem:
            data = await client.download_media(m, file=bytes, thumb=-1 if thumb else None)
        if not data:
            raise web.HTTPNotFound()
        ctype = "image/jpeg" if (thumb or kind == "photo") else (mime or "application/octet-stream")
        entry = (data, ctype, name)
        cache_put(key, entry)

    data, ctype, name = entry
    headers = {"Cache-Control": "private, max-age=3600", "Accept-Ranges": "bytes"}
    if dl:
        headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(name or "file")
    rng = request.headers.get("Range")
    if rng and rng.startswith("bytes="):
        try:
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else 0
            end = min(int(b), len(data) - 1) if b else len(data) - 1
            if start > end:
                raise ValueError
            headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
            return web.Response(status=206, body=data[start:end + 1], content_type=ctype, headers=headers)
        except ValueError:
            pass
    return web.Response(body=data, content_type=ctype, headers=headers)


async def ws_handler(request):
    token, _ = get_client(request)
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    sockets.setdefault(token, set()).add(ws)
    try:
        async for _ in ws:
            pass
    finally:
        sockets.get(token, set()).discard(ws)
    return ws


async def index(request):
    return web.Response(text=INDEX_HTML, content_type="text/html")


# ---------------------------------------------------------------- start / stop
async def restore_sessions(app):
    if not os.path.exists(SESSIONS_FILE):
        return
    try:
        with open(SESSIONS_FILE) as f:
            saved = json.load(f)
    except Exception:
        log.exception("sessions.json o'qilmadi")
        return
    for token, session in saved.items():
        try:
            client = make_client(session)
            await client.connect()
            if await client.is_user_authorized():
                clients[token] = client
                attach_updates(token, client)
            else:
                await client.disconnect()
        except Exception:
            log.exception("Sessiyani tiklab bo'lmadi")
    log.info("Tiklangan sessiyalar: %d", len(clients))


async def close_clients(app):
    for c in list(clients.values()) + [p["client"] for p in pending.values()]:
        try:
            await c.disconnect()
        except Exception:
            pass


def make_app():
    app = web.Application(middlewares=[cors_mw, errors_mw])
    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.router.add_post("/api/send_code", send_code)
    app.router.add_post("/api/sign_in", sign_in)
    app.router.add_get("/api/me", me)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/dialogs", dialogs)
    app.router.add_get("/api/messages", messages)
    app.router.add_post("/api/send", send)
    app.router.add_post("/api/edit", edit)
    app.router.add_post("/api/delete", delete)
    app.router.add_post("/api/upload", upload)
    app.router.add_get("/api/media", media)
    app.router.add_get("/api/ws", ws_handler)
    app.router.add_get("/api/avatar/{chat_id}", avatar)
    app.on_startup.append(restore_sessions)
    app.on_cleanup.append(close_clients)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
