from locust import HttpUser, between, task


class ReadinessSmokeUser(HttpUser):
    wait_time = between(1, 2)

    @task
    def readiness(self):
        self.client.get("/health/ready")
