# Oracle web client — React/Vite build served by nginx, which also forwards
# the game websocket to the server container. Build context is the repo root
# (so this file is referenced as `dockerfile: web/Dockerfile`).
FROM node:22-alpine AS build
WORKDIR /app

COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./

# "auto" makes the built page resolve its ws:// URL from whatever host and
# scheme it is served over, via nginx's /ws proxy below — so one image works
# on any host/port with no rebuild. Override at build time to bake in a
# fixed URL instead.
ARG VITE_SERVER_URI=auto
ENV VITE_SERVER_URI=$VITE_SERVER_URI
RUN npm run build

FROM nginx:1.27-alpine
COPY web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
