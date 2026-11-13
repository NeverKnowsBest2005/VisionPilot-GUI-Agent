<script setup>
import { ref } from 'vue'
const emit = defineEmits(['submit','demo'])
const goal = ref(''), maxSteps = ref('')
function run() { if (!goal.value.trim()) return; emit('submit', goal.value.trim(), maxSteps.value); goal.value = '' }
const demos = [['notes','记事本'],['explorer','文件浏览'],['settings','系统设置'],['english','英文任务']]
</script>
<template><section class="panel composer"><div class="panel-title"><div><span class="eyebrow">NEW TASK</span><h2>创建任务</h2></div><span class="live-dot">● 服务就绪</span></div><textarea v-model="goal" @keydown.ctrl.enter="run" @keydown.meta.enter="run" placeholder="描述你希望 Agent 完成的桌面任务…"></textarea><div class="composer-actions"><label>最大步数 <input v-model="maxSteps" type="number" min="1" max="100" placeholder="默认" /></label><span class="grow"></span><button class="primary" :disabled="!goal.trim()" @click="run">运行任务 <span>↗</span></button></div><div class="demo-row"><span>快速演示</span><button v-for="d in demos" :key="d[0]" class="demo" @click="emit('demo',d[0])">{{ d[1] }}</button></div></section></template>
