<script setup>
import { computed } from 'vue'
const props = defineProps({ events:Array })
const labels = { task_started:'任务开始', observation:'环境观察', plan_created:'计划生成', step_started:'步骤开始', step_finished:'步骤完成', recovery:'执行恢复', plan_revised:'计划调整', task_finished:'任务结束', task_status:'状态更新' }
const rows = computed(() => [...(props.events||[])].reverse())
function summary(e) { return ({ observation:`${e.active_window||'桌面'} · ${e.elements??0} 个元素`, step_started:`${e.tool||''} · 第 ${(e.index??0)+1} 步`, step_finished:`${e.tool||''} ${e.ok?'完成':'失败'} · ${e.observation||e.error||''}`, plan_created:`${e.step_count??e.steps?.length??0} 个步骤 · ${e.source||''}`, recovery:`${e.action||''} · ${e.reason||''}`, task_started:e.goal||'', task_finished:`${e.success?'成功':'未完成'} · ${e.status||''}`, task_status:e.status||'' })[e.type] || e.message || '' }
</script>
<template><section class="panel feed-panel"><div class="panel-title"><div><span class="eyebrow">LIVE TRACE</span><h2>实时事件流</h2></div><span class="live-dot">● LIVE</span></div><div class="event-feed"><div v-if="!rows.length" class="empty">等待 Agent 事件…</div><div v-for="(e,i) in rows" :key="`${e.timestamp}-${i}`" class="event-row" :class="{ danger:e.ok===false||e.success===false, success:e.ok===true||e.success===true }"><time>{{ (e.timestamp||'').slice(11,19) }}</time><b>{{ labels[e.type]||e.type }}</b><span>{{ summary(e) }}</span></div></div></section></template>
