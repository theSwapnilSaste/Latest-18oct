# Use a Python 3.12.3 Alpine base image
FROM python:3.12-alpine3.20

# Set the working directory
WORKDIR /app

# Copy all files from the current directory to the container's /app directory
COPY . .

# Install necessary dependencies with optimized build settings
RUN apk add --no-cache \
    gcc \
    libffi-dev \
    musl-dev \
    ffmpeg \
    aria2 \
    make \
    g++ \
    cmake \
    && \
    # Download and build Bento4 with optimized compilation
    wget -q https://github.com/axiomatic-systems/Bento4/archive/v1.6.0-639.zip && \
    unzip -q v1.6.0-639.zip && \
    cd Bento4-1.6.0-639 && \
    mkdir build && \
    cd build && \
    cmake .. -DCMAKE_BUILD_TYPE=Release && \
    make -j$(nproc) && \
    cp mp4decrypt /usr/local/bin/ &&\
    cd ../.. && \
    rm -rf Bento4-1.6.0-639 v1.6.0-639.zip

# Install Python dependencies with optimized pip settings
RUN pip3 install --no-cache-dir --upgrade pip \
    && pip3 install --no-cache-dir --upgrade -r sainibots.txt \
    && python3 -m pip install -U yt-dlp

# Create optimized aria2 configuration for maximum speed
RUN mkdir -p /etc/aria2 && \
    echo "max-download-limit=0" > /etc/aria2/aria2.conf && \
    echo "max-overall-download-limit=0" >> /etc/aria2/aria2.conf && \
    echo "max-concurrent-downloads=16" >> /etc/aria2/aria2.conf && \
    echo "split=64" >> /etc/aria2/aria2.conf && \
    echo "min-split-size=1M" >> /etc/aria2/aria2.conf && \
    echo "max-connection-per-server=16" >> /etc/aria2/aria2.conf && \
    echo "max-tries=0" >> /etc/aria2/aria2.conf && \
    echo "retry-wait=1" >> /etc/aria2/aria2.conf && \
    echo "connect-timeout=5" >> /etc/aria2/aria2.conf && \
    echo "timeout=5" >> /etc/aria2/aria2.conf && \
    echo "lowest-speed-limit=0" >> /etc/aria2/aria2.conf && \
    echo "disk-cache=64M" >> /etc/aria2/aria2.conf && \
    echo "file-allocation=prealloc" >> /etc/aria2/aria2.conf && \
    echo "no-file-allocation-limit=64M" >> /etc/aria2/aria2.conf && \
    echo "allow-overwrite=true" >> /etc/aria2/aria2.conf && \
    echo "auto-file-renaming=false" >> /etc/aria2/aria2.conf && \
    echo "continue=true" >> /etc/aria2/aria2.conf

# Create performance tuning script
RUN echo '#!/bin/sh' > /tune-performance.sh && \
    echo 'echo "Applying performance optimizations..."' >> /tune-performance.sh && \
    echo 'sysctl -w net.core.rmem_max=268435456' >> /tune-performance.sh && \
    echo 'sysctl -w net.core.wmem_max=268435456' >> /tune-performance.sh && \
    echo 'sysctl -w net.ipv4.tcp_rmem="4096 87380 268435456"' >> /tune-performance.sh && \
    echo 'sysctl -w net.ipv4.tcp_wmem="4096 65536 268435456"' >> /tune-performance.sh && \
    echo 'sysctl -w net.core.netdev_max_backlog=250000' >> /tune-performance.sh && \
    echo 'sysctl -w net.ipv4.tcp_no_metrics_save=1' >> /tune-performance.sh && \
    echo 'sysctl -w net.ipv4.tcp_congestion_control=bbr' >> /tune-performance.sh && \
    echo 'ulimit -n 1048576' >> /tune-performance.sh && \
    echo 'echo "Performance tuning completed"' >> /tune-performance.sh && \
    chmod +x /tune-performance.sh

# Set environment variables for maximum performance
ENV MAX_DOWNLOAD_SPEED=0
ENV ARIA2_CONFIG=/etc/aria2/aria2.conf
ENV UV_THREADPOOL_SIZE=32
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Set the command to run the application with performance tuning
CMD ["sh", "-c", "/tune-performance.sh && gunicorn app:app --workers 4 --threads 8 --bind 0.0.0.0:8000 & python3 -O main.py"]
