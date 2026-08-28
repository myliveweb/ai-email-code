import type { NextConfig } from "next";

const backendUrl = process.env.BACKEND_URL ?? "http://127.0.0.1:4000";

const nextConfig: NextConfig = {
  turbopack: { root: __dirname },
  // адрес машины в локальной сети: с ноутбука Босса UI открывается по нему,
  // и без записи в списке dev-сервер режет свои же запросы как чужой origin
  allowedDevOrigins: ["localhost", "127.0.0.1", "192.168.1.123"],
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backendUrl}/api/:path*` }];
  },
};

export default nextConfig;
