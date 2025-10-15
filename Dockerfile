# Stage 1: Build the virtual environment
FROM python:3.9-slim as builder

WORKDIR /app

# Create a virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2: Build the final image
FROM python:3.9-slim

WORKDIR /app

# Copy the virtual environment from the builder stage
COPY --from=builder /opt/venv /opt/venv

# Set the path to use the virtual environment
ENV PATH="/opt/venv/bin:$PATH"

# Copy the application code
COPY . .

# Run collectstatic to gather all static files into the STATIC_ROOT directory
RUN python manage.py collectstatic --no-input

# Expose the port Gunicorn will run on
EXPOSE 8000

# Run the Gunicorn server
CMD ["gunicorn", "--bind", ":8000", "LazyOne.wsgi"]