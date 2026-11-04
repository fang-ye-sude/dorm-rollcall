/* B座点名·管理中心 逻辑 */
const API = '';            // 同源（应用容器 :8100 / 经 example.com/rollcall/）
const TOKEN = 'CHANGE_ME_TOKEN';
const $ = id => document.getElementById(id);
const admin = () => $('adminKey').value.trim() || sessionStorage.getItem('rcAdmin') || '';

async function jget(u) { const r = await fetch(u); return r.json(); }
async function jpost(u, body) {
  const r = await fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  return r.json();
}

async function refresh() {
  const d = await jget(`/api/history?admin=${encodeURIComponent(admin())}`);
  if (!d.ok) { $('histBox').innerHTML = '<span class=err>口令不对或未填（管理口令在右上角输入）</span>'; return; }
  sessionStorage.setItem('rcAdmin', admin());
  const c = d.cfg || {};
  $('cfgNode').value = c.node || ''; $('cfgSheet').value = c.sheet || 's1'; $('cfgWeek').value = c.daterange || '';
  $('histStat').textContent = `共 ${d.items.length} 件`;
  $('histBox').innerHTML = '<table><tr><th>文件</th><th>类</th><th>日期</th><th>概览</th><th>落位预览</th><th>操作</th></tr>' +
    d.items.map(it => {
      if (it.type === 'marked') {
        const p = it.plan;
        const pv = p ? `块${p.block}·行${p.row} 请假${p.leaveN}<br><span class="plan">DS${p.counts.DS} DB${p.counts.DB} XS${p.counts.XS} XB${p.counts.XB}${p.weekend?' ·周末':''}</span>` : '不在周日~周四';
        return `<tr><td>${it.f}</td><td><span class="tag mk">标记</span></td><td>${it.d||''}</td>
          <td>${it.n}人 · ref:${it.ref||'—'}</td><td class="plan">${pv}</td>
          <td><button class="mini" onclick="fillNow('${it.f}')">立刻填入</button>
              <a class="mini" href="/raw/${it.f}?token=${TOKEN}">查看</a>
              <button class="mini" onclick="del('${it.f}')">删</button></td></tr>`;
      }
      return `<tr><td>${it.f}</td><td><span class="tag rl">回执</span></td><td>${it.d||''}</td>
        <td>应${it.t} 实${it.p} 缺${it.a}</td><td></td>
        <td><a class="mini" href="/raw/${it.f}?token=${TOKEN}">查看</a>
            <button class="mini" onclick="del('${it.f}')">删</button></td></tr>`;
    }).join('') + '</table>';
  $('logBox').textContent = (d.log || []).join('') || '（尚无填报流水）';
}
async function fillNow(f) {
  if (!confirm('按这份标记件立刻重算并写入在线表（覆盖同位置旧值）？')) return;
  const r = await jpost(`/api/fill-now?admin=${encodeURIComponent(admin())}`, { f });
  alert(r.ok ? '已执行，看日志区' : '失败：' + (r.error || ''));
  refresh();
}
async function del(f) {
  if (!confirm('隐藏这份上传件？（挪 .trash，可找回，不物理销毁）')) return;
  const r = await fetch(`/api/delete?admin=${encodeURIComponent(admin())}&name=${encodeURIComponent(f)}`, { method: 'DELETE' }).then(x => x.json());
  alert(r.ok ? '已隐藏' : '失败：' + (r.error || ''));
  refresh();
}
$('cfgSave').onclick = async () => {
  const r = await jpost(`/api/config?admin=${encodeURIComponent(admin())}`,
    { node: $('cfgNode').value.trim(), sheet: $('cfgSheet').value.trim() || 's1', daterange: $('cfgWeek').value.trim() });
  $('cfgMsg').textContent = r.ok ? '已保存 ✓' : '失败';
};
$('btnStudents').onclick = async () => {
  $('stuCard').classList.remove('hidden');
  const d = await jget('/api/students');
  const rows = (d.students || []).filter(s => s.absent.length);
  $('stuBox').innerHTML = rows.length ? '<table><tr><th>姓名</th><th>班级</th><th>房间</th><th>缺勤记录（近→远）</th></tr>' +
    rows.map(s => `<tr><td>${s.name}</td><td>${s.cls}</td><td>${s.room}</td>
      <td>${s.absent.map(a => `<span class="tag${a.q && a.q[0]==='X' ? '' : ' rl'}" title="${a.f}">${a.date.slice(5)} ${a.q}</span>`).join(' ')}</td></tr>`).join('') + '</table>'
    : '尚无缺勤史';
};
$('refresh').onclick = refresh;
if (!admin()) $('adminKey').placeholder = '管理口令（必填）';
refresh();

// ---------- 住宿表：预演 / 确认 / 自动源 ----------
const $f = id => document.getElementById(id);
function toastMsg(el, t) { if (el) el.textContent = t || ''; }

function fileB64(input) {
  return new Promise(resolve => {
    const f = input.files && input.files[0];
    if (!f) return resolve(null);
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(',')[1] || null);
    r.readAsDataURL(f);
  });
}

async function rosterSend(confirm) {
  const [b, a, m, fl] = await Promise.all([fileB64($f('rb')), fileB64($f('ra')), fileB64($f('rm')), fileB64($f('rf'))]);
  if (!b && !a) { toastMsg($f('rMsg'), '至少选一张分布图'); return null; }
  toastMsg($f('rMsg'), confirm ? '正在写入…' : '正在预演…');
  const body = { b, a, male: m, female: fl, confirm };
  const r = await jpost(`/api/roster/import?admin=${encodeURIComponent(admin())}`, body);
  if (!r || !r.ok) { toastMsg($f('rMsg'), '失败：' + ((r && (r.error || r.raw)) || '未知')); return null; }
  const lines = Object.entries(r.floors || {}).map(([k, v]) => {
    const add = Object.keys(v.added || {}).length, rm = Object.keys(v.removed || {}).length;
    return `${k}: ${v.rooms} 房 ${v.people} 人` + (add ? `（+${add} 房有新增）` : '') + (rm ? `（${rm} 房有人减少）` : '');
  });
  $f('rBox').textContent = (confirm ? '已写入：\n' : '预演结果：\n') + lines.join('\n');
  toastMsg($f('rMsg'), confirm ? '已更新名册' : '预演完成，确认无误再点「确认更新」');
  $f('rApply').disabled = confirm;
  return r;
}

$f('rPreview').onclick = () => rosterSend(false);
$f('rApply').onclick = () => rosterSend(true);

async function loadSources() {
  const r = await jget(`/api/roster/sources?admin=${encodeURIComponent(admin())}`);
  if (!r || !r.ok) { toastMsg($f('srcMsg'), '读取失败（先填管理口令）'); return; }
  const cfg = r.cfg || {};
  const map = {};
  (cfg.sources || []).forEach(s => map[s.key] = s.url);
  $f('srcB').value = map.b || ''; $f('srcA').value = map.a || '';
  $f('srcM').value = map.male || ''; $f('srcF').value = map.female || '';
  $f('srcApply').checked = !!cfg.apply;
  const st = r.state || {};
  const last = Object.entries(st).map(([k, v]) => `${k} ${v.at}`).join(' · ');
  toastMsg($f('srcMsg'), last ? '上次抓到：' + last : '还没有抓过');
  $f('srcLog').textContent = (r.log || []).join('');
}

$f('srcSave').onclick = async () => {
  const sources = [];
  if ($f('srcB').value.trim()) sources.push({ key: 'b', url: $f('srcB').value.trim() });
  if ($f('srcA').value.trim()) sources.push({ key: 'a', url: $f('srcA').value.trim() });
  if ($f('srcM').value.trim()) sources.push({ key: 'male', url: $f('srcM').value.trim() });
  if ($f('srcF').value.trim()) sources.push({ key: 'female', url: $f('srcF').value.trim() });
  const r = await jpost(`/api/roster/sources?admin=${encodeURIComponent(admin())}`,
    { sources, apply: $f('srcApply').checked });
  toastMsg($f('srcMsg'), r && r.ok ? '已保存 ' + sources.length + ' 个源' : '保存失败');
};

$f('srcCheck').onclick = async () => {
  toastMsg($f('srcMsg'), '正在检查…');
  const r = await jpost(`/api/roster/check?admin=${encodeURIComponent(admin())}`, {});
  $f('srcLog').textContent = (r && r.log || []).join('') || (r && (r.out || r.err)) || '';
  toastMsg($f('srcMsg'), r && r.ok ? '检查完成' : '检查失败');
};

loadSources();
