// 演示用四端身份。密码不在此保存，切换由后端 /api/auth/demo-switch 完成。
export const DEMO_ROLES = [
  { role: 'ADMIN', label: '管理员', username: 'admin', desc: '全局管理 · 提问人身份 · 发起私聊' },
  { role: 'ENGINEER', label: '工程师', username: 'engineer', desc: '故障排查 · 接单处置' },
  { role: 'USER', label: '普通用户', username: 'user', desc: '发起提问 · 更新工作日志' },
  { role: 'LEADER', label: '领导', username: 'leader', desc: '审计与报表 · 全过程可见' },
]

export const ROLE_LABEL = Object.fromEntries(DEMO_ROLES.map((r) => [r.role, r.label]))
