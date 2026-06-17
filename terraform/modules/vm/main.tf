# Latest Canonical Ubuntu 22.04 LTS (Jammy) AMD64 server AMI in the target region.
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# Register the operator's SSH public key. The key lands on the AMI's default user
# (ubuntu) so SSH works as ${admin_username}@<public_ip>. Password auth is disabled
# by default on Ubuntu cloud images.
resource "aws_key_pair" "this" {
  key_name   = "key-${var.name_suffix}"
  public_key = var.ssh_public_key
  tags       = var.tags
}

resource "aws_instance" "this" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [var.security_group_id]
  key_name                    = aws_key_pair.this.key_name
  user_data                   = var.user_data
  user_data_replace_on_change = true

  root_block_device {
    volume_type = "gp3"
    volume_size = 30
    tags        = merge(var.tags, { Name = "osdisk-${var.name_suffix}" })
  }

  tags = merge(var.tags, { Name = "vm-${var.name_suffix}" })
}

# Static public IP so the address survives instance reboots during a demo.
resource "aws_eip" "this" {
  instance = aws_instance.this.id
  domain   = "vpc"
  tags     = merge(var.tags, { Name = "eip-${var.name_suffix}" })
}
