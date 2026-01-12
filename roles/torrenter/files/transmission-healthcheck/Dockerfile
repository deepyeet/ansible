# Use an official lightweight Python image.
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the check script into the container
COPY check.py .

# Set environment variables for Python to run in unbuffered mode
ENV PYTHONUNBUFFERED 1

# Command to run the script
CMD ["python", "check.py"]
