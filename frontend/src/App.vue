<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import TaskComposer from './components/TaskComposer.vue'
import TaskList from './components/TaskList.vue'
import TaskDetail from './components/TaskDetail.vue'
import EventFeed from './components/EventFeed.vue'

const health = ref(null), tasks = ref([]), selected = ref(null), detail = ref(null)
const stats = ref({}), skills = ref([]), tools = ref([]), events = ref([]), error = ref('')
let socket, taskTimer, statsTimer, reconnectTimer
const chips = computed(() => health.value ? [
  `${health.value.execution_mode === 'live' ? '实时桌面' : '模拟模式'}`,
  health.value.llm_ready ? 'LLM 已连接' : '规则规划器',
  `工具 ${health.value.tools_available}/${health.value.tools_total}`,
  `历史任务 ${health.value.tasks_total}`,
] : ['连接中…'])
async function api(path, options) {
  const res = await fetch(path, options)
  if (!res.ok) { const body = await res.json().catch(() => ({})); throw new Error(body.detail || res.statusText) }
  return res.json()
}
async function refreshHealth() { health.value = await api('/api/health') }
async function refreshTasks() {
  tasks.value = await api('/api/tasks?limit=40')
  if (selected.value) await refreshDetail(selected.value, true)
}
async function refreshDetail(id, quiet = false) {
  try { detail.value = await api(`/api/tasks/${id}`) }
  catch (e) { if (!quiet) error.value = e.message }
}
async function refreshStats() {
  const [s, k, t] = await Promise.all([api('/api/stats'), api('/api/skills?limit=20'), api('/api/tools')])
  stats.value = s; skills.value = k.skills; tools.value = t.tools
}
function selectTask(id) { selected.value = id; refreshDetail(id); events.value = [] }
function onEvent(e) {
  if (e.type === 'pong') return
  if (e.type === 'snapshot') { e.events?.forEach(onEvent); return }
  events.value = [...events.value.slice(-299), e]
  if (['task_started','task_finished','task_status'].includes(e.type)) {
    refreshTasks(); refreshHealth(); refreshStats()
  }
}
function connect() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
  socket = new WebSocket(`${scheme}://${location.host}/ws/events${selected.value ? `?task_id=${selected.value}` : ''}`)
  socket.onmessage = e => { try { onEvent(JSON.parse(e.data)) } catch {} }
  socket.onclose = () => { reconnectTimer = setTimeout(connect, 2000) }
}
async function submit(goal, maxSteps) {
  error.value = ''
  try {
    const created = await api('/api/tasks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ goal, ...(maxSteps ? { max_steps: Number(maxSteps) } : {}) }) })
    selected.value = created.task_id; await refreshTasks(); await refreshDetail(created.task_id); connect()
  } catch (e) { error.value = `提交失败：${e.message}` }
}
async function demo(scenario) {
  try { const task = await api('/api/demo', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scenario }) }); selected.value = task.task_id; await refreshTasks(); await refreshDetail(task.task_id); connect() }
  catch (e) { error.value = `演示启动失败：${e.message}` }
}
onMounted(async () => {
  try { await Promise.all([refreshHealth(), refreshTasks(), refreshStats()]); connect() }
  catch (e) { error.value = `连接服务失败：${e.message}` }
  taskTimer = setInterval(refreshTasks, 4000); statsTimer = setInterval(refreshStats, 15000)
})
onUnmounted(() => { clearInterval(taskTimer); clearInterval(statsTimer); clearTimeout(reconnectTimer); socket?.close() })
</script>

<template>
  <div class="shell">
    <header class="topbar"><a class="brand" href="#"><span class="mark">VP</span><span><b>VisionPilot</b><small>桌面智能体控制台</small></span></a><div class="health-chips"><span v-for="chip in chips" :key="chip" class="chip">{{ chip }}</span></div></header>
    <main>
      <section class="intro"><div><p class="eyebrow">AUTONOMOUS DESKTOP AGENT</p><h1>把目标交给 Agent</h1><p class="sub">观察环境、规划步骤、执行操作，并验证最终结果。</p></div><div class="flow"><span>观察</span><i>→</i><span>规划</span><i>→</i><span>执行</span><i>→</i><span>验证</span><i>→</i><span>学习</span></div></section>
      <p v-if="error" class="alert">{{ error }} <button class="link" @click="error=''">关闭</button></p>
      <section class="workspace-grid"><div class="left-stack"><TaskComposer @submit="submit" @demo="demo"/><TaskList :tasks="tasks" :selected="selected" @select="selectTask"/></div><div class="right-stack"><TaskDetail :task="detail" :stats="stats" :skills="skills" :tools="tools"/><EventFeed :events="events"/></div></section>
    </main>
    <footer>VisionPilot <span>·</span> 本地运行的自主桌面自动化</footer>
  </div>
</template>
