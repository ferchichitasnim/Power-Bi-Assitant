/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  webpack: (config, { dev }) => {
    if (dev) {
      // Avoid filesystem cache writes that can fail with EBUSY on Windows.
      config.cache = false;
    }
    return config;
  },
};

export default nextConfig;
