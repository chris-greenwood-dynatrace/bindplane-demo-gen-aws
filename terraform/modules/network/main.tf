resource "aws_vpc" "this" {
  cidr_block           = "10.0.0.0/24"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(var.tags, { Name = "vpc-${var.name_suffix}" })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "igw-${var.name_suffix}" })
}

resource "aws_subnet" "this" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = "10.0.0.0/24"
  map_public_ip_on_launch = false
  tags                    = merge(var.tags, { Name = "snet-${var.name_suffix}" })
}

resource "aws_route_table" "this" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = merge(var.tags, { Name = "rt-${var.name_suffix}" })
}

resource "aws_route_table_association" "this" {
  subnet_id      = aws_subnet.this.id
  route_table_id = aws_route_table.this.id
}

resource "aws_security_group" "this" {
  name        = "sg-${var.name_suffix}"
  description = "bindplane-demo: SSH in from admin CIDR; all egress for OpAMP wss + Dynatrace OTLP + package pulls."
  vpc_id      = aws_vpc.this.id
  tags        = merge(var.tags, { Name = "sg-${var.name_suffix}" })

  # ── Inbound: SSH from the admin CIDR only ──────────────────────────────────
  ingress {
    description = "SSH from admin CIDR"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.admin_source_cidr]
  }

  # ── Outbound: all (OpAMP wss/443, Dynatrace OTLP https/443, apt + docker pulls) ──
  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
