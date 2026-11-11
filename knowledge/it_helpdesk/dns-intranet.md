# DNS 与内网域名解析

典型现象：外网网站正常，但内网系统（如 GitLab、Jira、内部门户）打不开，或提示“找不到服务器”。

排查步骤：
1. 远程办公时确认 VPN 已连接；内网域名只有在办公网或 VPN 下才能解析。
2. 用浏览器直接访问其他内网系统，判断是单个系统故障还是全部内网不可达。单个系统打不开，先看 IT 服务状态公告。
3. 刷新 DNS 缓存：Windows 执行 ipconfig /flushdns；macOS 执行 sudo dscacheutil -flushcache 后再执行 sudo killall -HUP mDNSResponder。
4. 检查是否手动设置过 DNS（例如 8.8.8.8）或安装了第三方加速、代理软件，这会导致内网域名无法解析，恢复为自动获取。
5. 检查 hosts 文件是否被修改过。不要自行在 hosts 中添加内网地址。

提交工单时附上：无法访问的完整地址、是否连了 VPN、nslookup 或 ping 该域名的结果截图。
