<script setup>
defineProps({ tasks: { type: Array, default: () => [] }, selected: String })
defineEmits(['select'])
const labels = { queued:'排队中', running:'执行中', completed:'已完成', failed:'失败', aborted:'已中止', cancelled:'已取消' }
</script>
<template><section class="panel"><div class="panel-title"><div><span class="eyebrow">RECENT ACTIVITY</span><h2>最近任务 <span class="count">{{ tasks.length }}</span></h2></div><span class="muted">自动刷新</span></div><div v-if="!tasks.length" class="empty">还没有任务，试试上方的演示场景。</div><button v-for="task in tasks" :key="task.task_id" class="task-row" :class="{ active: selected===task.task_id }" @click="$emit('select',task.task_id)"><span class="task-icon">{{ task.success ? '✓' : task.status==='running' ? '◌' : '↗' }}</span><span class="task-copy"><b>{{ task.goal }}</b><small>#{{ task.task_id }} · {{ task.steps }} 步 · {{ (task.created_at || '').replace('T',' ').slice(0,16) }}</small></span><span class="status" :class="task.status">{{ labels[task.status] || task.status }}</span></button></section></template>
