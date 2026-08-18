variable "name" {}
module "child" { source = "./child" }
resource "aws_instance" "web" { ami = var.name }
