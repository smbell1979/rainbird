# syntax=docker/dockerfile:1

# Modelled on the weather app's image: node:24-slim, full production
# node_modules rather than Next's standalone output.
FROM node:24-slim AS base
WORKDIR /app

# ---- dependencies -----------------------------------------------------------
FROM base AS deps
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# ---- build ------------------------------------------------------------------
FROM base AS build
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# ---- production dependencies ------------------------------------------------
FROM base AS proddeps
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

# ---- runtime ----------------------------------------------------------------
FROM base AS runtime
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

COPY --from=proddeps /app/node_modules ./node_modules
COPY --from=build /app/.next ./.next
COPY --from=build /app/public ./public
COPY --from=build /app/package.json ./package.json
COPY --from=build /app/next.config.ts ./next.config.ts

# node is uid 1000, as is scott on the NUC.
USER node
# All interfaces: the page is reached on the LAN as http://amethyst:3004.
CMD ["npx", "next", "start", "-p", "3004", "-H", "0.0.0.0"]
