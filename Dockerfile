# Runs the real platform code (webapp GUI, CLI demo, test suite) in a
# container -- not a staged/mocked version, the same OrchestrationService
# and adapters everything else in this repo uses. See README.md's "What's
# real vs. illustrative": this image does NOT attempt to build cpp_nodes/
# (a real ROS2 toolchain) -- that directory is explicitly illustrative,
# written for API-shape correctness, and was never meant to be compiled
# in this repo's own environment, containerized or not.

FROM python:3.11-slim

WORKDIR /app

# Dependencies first, in their own layer, so `docker build` doesn't
# reinstall them on every source change.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runs the Flask GUI reachable from outside the container; webapp/app.py
# reads these at startup (defaults to loopback-only for local, non-Docker
# use -- see its own comment at the bottom of the file).
ENV WEBAPP_HOST=0.0.0.0
ENV WEBAPP_PORT=5055
EXPOSE 5055

# A non-root user is standard container practice -- but this app writes
# real files at runtime (OrchestrationService's default delivery_output_dir
# creates deliveries/*.zip relative to cwd the first time a mission
# completes), so /app must actually be owned by the user that runs it, not
# just exist. chown here, not a volume mount, since these are small
# ephemeral demo artifacts, not data that needs to survive a container
# restart.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Overridable: `docker run <image> python -m pytest tests/ -v` or
# `docker run <image> python demo/run_demo.py` both work against the
# same image without a rebuild.
CMD ["python", "webapp/app.py"]
