Create the Maven proxy config:
mkdir -p ~/.m2
cat > ~/.m2/settings.xml <<'EOF'
<settings>
  <proxies>
    <proxy>
      <id>http-proxy</id>
      <active>true</active>
      <protocol>http</protocol>
      <host>172.31.8.100</host>
      <port>8090</port>
      <nonProxyHosts>localhost|127.0.0.1|10.0.8.247|10.0.16.236|172.31.8.100</nonProxyHosts>
    </proxy>
    <proxy>
      <id>https-proxy</id>
      <active>true</active>
      <protocol>https</protocol>
      <host>172.31.8.100</host>
      <port>8090</port>
      <nonProxyHosts>localhost|127.0.0.1|10.0.8.247|10.0.16.236|172.31.8.100</nonProxyHosts>
    </proxy>
  </proxies>
</settings>
EOF
